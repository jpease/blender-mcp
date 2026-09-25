"""
Rows guarding which integration tools a session is offered, and what a call to a withheld one does.

Label prefixes: `integrations:`.
"""

from .common import INTEGT, SERVER_APP, SERVER_CORE_TOOL, SERVER_INTEGRATIONS, Revert

_LISTED = f"{INTEGT}::test_the_tool_list_carries_an_integrations_tools_unless_the_handshake_shows_it_disabled"
_ANNOUNCED = f"{INTEGT}::test_a_session_that_listed_tools_is_told_when_a_handshake_withholds_some"

ROWS: list[Revert] = [
    Revert(
        "integrations: a disabled integration's tools stay in the tool list",
        SERVER_APP,
        "        return [tool for tool in await super().list_tools() if tool.name not in withheld]",
        "        return await super().list_tools()",
        (f"{_LISTED}[disabled]", _ANNOUNCED),
    ),
    Revert(
        "integrations: every integration is withheld until a handshake proves it enabled",
        SERVER_INTEGRATIONS,
        "    if handshake is None or not handshake.capabilities:\n        return frozenset()\n",
        "    if handshake is None or not handshake.capabilities:\n        return frozenset(INTEGRATIONS)\n",
        (f"{_LISTED}[no-handshake-yet]", _ANNOUNCED),
    ),
    Revert(
        "integrations: a call to a disabled integration's tool is dispatched anyway",
        SERVER_APP,
        "            if refusal is not None:\n                raise ToolError(refusal)",
        "            if False:\n                raise ToolError(refusal)",
        (f"{INTEGT}::test_a_call_to_a_disabled_integration_is_refused_before_dispatch",),
    ),
    Revert(
        "integrations: a cached 'disabled' is trusted, so ticking the checkbox never takes effect",
        SERVER_INTEGRATIONS,
        "        handshake = force_addon_handshake(get_blender_connection())",
        "        handshake = handshake or force_addon_handshake(get_blender_connection())",
        (f"{INTEGT}::test_an_integration_enabled_since_the_cached_handshake_is_not_refused",),
    ),
    Revert(
        "integrations: a session is never told its tool list changed",
        SERVER_APP,
        "            await session.send_tool_list_changed()",
        "            pass",
        (_ANNOUNCED,),
    ),
    Revert(
        "integrations: initialize says the tool list never changes",
        SERVER_APP,
        "NotificationOptions(tools_changed=True)",
        "NotificationOptions()",
        (f"{INTEGT}::test_the_server_tells_clients_its_tool_list_can_change",),
    ),
    Revert(
        "integrations: a tool lookup calls a withheld tool plainly callable",
        SERVER_CORE_TOOL,
        "    if tool_name in mounted and integration in withheld:",
        "    if False:",
        (f"{INTEGT}::test_a_tool_lookup_names_the_checkbox_a_withheld_tool_is_waiting_on[disabled]",),
    ),
    Revert(
        "integrations control: an enabled integration is withheld as if it were disabled",
        SERVER_INTEGRATIONS,
        "    return frozenset(provider for provider in INTEGRATIONS if not advertises(handshake, provider))",
        "    return frozenset(INTEGRATIONS)",
        (
            f"{_LISTED}[enabled]",
            f"{INTEGT}::test_an_integration_enabled_since_the_cached_handshake_is_not_refused",
            f"{INTEGT}::test_a_tool_lookup_names_the_checkbox_a_withheld_tool_is_waiting_on[enabled]",
        ),
    ),
]
