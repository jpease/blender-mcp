"""
Rows guarding which tool bundles a session lists, and what enabling one registers.

Label prefixes: `toolsets:`.
"""

from .common import SERVER_APP, SERVER_CORE_TOOL, SERVER_DOCUMENTATION, SERVER_TOOLSETS_RUNTIME, TOOLSETST, Revert

_FRESH = f"{TOOLSETST}::test_a_fresh_session_lists_the_env_selection"
_ENABLE = f"{TOOLSETST}::test_enabling_a_bundle_lists_its_tools_and_announces_the_change"
_SECOND = f"{TOOLSETST}::test_a_second_session_is_unaffected_and_refused_with_the_bundle_named"
_UNKNOWN = f"{TOOLSETST}::test_an_unregistered_tool_is_unknown_and_points_at_get_addon_status"
_CORE = f"{TOOLSETST}::test_core_cannot_be_disabled_and_an_unknown_name_lists_the_valid_ones"
_WITHHELD = f"{TOOLSETST}::test_withheld_integrations_stay_hidden_after_enable"
_STATUS = f"{TOOLSETST}::test_get_addon_status_reports_per_session"
_TWICE = f"{TOOLSETST}::test_enabling_twice_registers_once_and_advertises_what_a_started_process_does"
_LIST = f"{TOOLSETST}::test_list_reports_each_bundles_tool_count_and_catalog_bytes"
_ONCE = f"{TOOLSETST}::test_the_documentation_pass_rewrites_a_tool_once_however_often_it_is_asked"

ROWS: list[Revert] = [
    Revert(
        "toolsets: manage_toolsets is never registered",
        SERVER_CORE_TOOL,
        "@mcp.tool()\nasync def manage_toolsets(",
        "async def manage_toolsets(",
        (f"{_FRESH}[None]", f"{_FRESH}[camera-rigs]", _ENABLE, _CORE),
    ),
    Revert(
        "toolsets: the startup selection registers core alone, whatever the env var names",
        SERVER_TOOLSETS_RUNTIME,
        "    return register_tool_modules(CORE_MODULES + tuple(module for name in STARTUP_BUNDLES",
        "    return register_tool_modules(CORE_MODULES + tuple(module for name in () ",
        (f"{_FRESH}[camera-rigs]",),
    ),
    Revert(
        "toolsets: a session's chosen bundles are ignored, so it keeps listing the startup selection",
        SERVER_APP,
        "        if enabled is None:\n            return registered - self._registered_on_demand\n",
        "        if True:\n            return registered - self._registered_on_demand\n",
        (_ENABLE, _SECOND, _WITHHELD, _STATUS, _TWICE),
    ),
    Revert(
        "toolsets: a tool another session enabled is listed to every session",
        SERVER_APP,
        "            return registered - self._registered_on_demand\n",
        "            return registered\n",
        (_SECOND, _STATUS),
    ),
    Revert(
        "toolsets: every registered tool is recorded as registered on demand",
        SERVER_TOOLSETS_RUNTIME,
        "    added = frozenset(registry.keys() - before)\n",
        "    added = frozenset(registry)\n",
        (_SECOND,),
    ),
    Revert(
        "toolsets: tools/list ignores the session's bundles",
        SERVER_APP,
        " if tool.name in listed and tool.name not in withheld]",
        " if tool.name not in withheld]",
        (_ENABLE, _SECOND),
    ),
    Revert(
        "toolsets: a call to a tool outside the session's bundles is dispatched",
        SERVER_APP,
        "            if (toolset_refusal := self._toolset_refusal(name)) is not None:",
        "            if (toolset_refusal := self._toolset_refusal(name)) is not None and False:",
        (_SECOND, _UNKNOWN),
    ),
    Revert(
        "toolsets: a session whose tool list changed is not told",
        SERVER_APP,
        "        if changed:\n            await session.send_tool_list_changed()\n",
        "        if changed:\n            pass\n",
        (_ENABLE,),
    ),
    Revert(
        "toolsets: ENABLE replaces the session's bundles instead of adding to them",
        SERVER_CORE_TOOL,
        "        current |= requested\n",
        "        current = requested\n",
        (_TWICE,),
    ),
    Revert(
        "toolsets: DISABLE core is accepted as a no-op",
        SERVER_CORE_TOOL,
        '    if action == "DISABLE" and CORE_BUNDLE in names:\n',
        "    if False:\n",
        (_CORE,),
    ),
    Revert(
        "toolsets: get_addon_status answers for the startup selection, not the calling session",
        SERVER_CORE_TOOL,
        "    mounted = _mounted_tool_names(_calling_session(ctx))\n",
        "    mounted = _mounted_tool_names()\n",
        (_STATUS,),
    ),
    Revert(
        "toolsets: the documentation pass rewrites a tool it already documented",
        SERVER_DOCUMENTATION,
        "        if name in _finalized:\n            continue\n",
        "",
        (_ONCE,),
    ),
    Revert(
        "toolsets: an unregistered bundle's bytes are measured in a core-only process",
        SERVER_TOOLSETS_RUNTIME,
        "    env = {**os.environ, TOOLSETS_ENV_VAR: ALL_SENTINEL}\n",
        "    env = {**os.environ, TOOLSETS_ENV_VAR: ''}\n",
        (_LIST,),
    ),
]
