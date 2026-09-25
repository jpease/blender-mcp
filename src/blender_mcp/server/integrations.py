"""
The optional integrations, and which mounted tools a disabled one withholds.

Poly Haven, Sketchfab and ND are mounted by `BLENDER_MCP_TOOLSETS` like any other bundle, but
the add-on serves their commands only while the open .blend ticks that integration's checkbox
(`command_registry.py`, `CommandSpec.provider`). A mounted tool whose integration is off can do
nothing but fail, so `app.py` leaves it out of tools/list once the handshake says so, and refuses
a call to it before anything is dispatched. Nothing is withheld while nothing is known: before
the first handshake, or from an add-on that advertises no capability list.

Which tools an integration gates is read from its tools module by `mount_map`, so a tool added
to `polyhaven.py` is gated without being listed here.
"""

import logging

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from ..addon_manager import AddonHandshake
from .connection import force_addon_handshake, get_blender_connection, get_last_handshake
from .mount_map import module_tool_names

logger = logging.getLogger("BlenderMCPServer")

Provider = Literal["polyhaven", "sketchfab", "nd"]


@dataclass(frozen=True)
class Integration:
    """
    One optional integration, as the server sees it.

    Attributes:
        label: Its name in prose.
        tools_module: The `server/tools` module whose every tool it gates.
        capability: A command the add-on advertises only while the integration is enabled.
        checkbox: The BlenderMCP sidebar checkbox that enables it.

    """

    label: str
    tools_module: str
    capability: str
    checkbox: str


INTEGRATIONS: Mapping[Provider, Integration] = MappingProxyType(
    {
        "polyhaven": Integration("Poly Haven", "polyhaven", "import_polyhaven_asset", "Use assets from Poly Haven"),
        "sketchfab": Integration("Sketchfab", "sketchfab", "import_sketchfab_model", "Use assets from Sketchfab"),
        "nd": Integration("ND", "nd", "nd_boolean", "Use ND (non-destructive hard-surface tools)"),
    }
)


def advertises(handshake: AddonHandshake, provider: Provider) -> bool:
    """
    Say whether the add-on serves an integration's commands for the open .blend.

    Args:
        handshake: What the add-on reported.
        provider: The integration asked about.

    Returns:
        bool: True when its gating command is among the advertised capabilities.

    """
    return INTEGRATIONS[provider].capability in handshake.capabilities


def withheld_providers(handshake: AddonHandshake | None) -> frozenset[Provider]:
    """
    Name the integrations a handshake shows to be disabled.

    Args:
        handshake: The latest handshake, or None when there has been none.

    Returns:
        frozenset[Provider]: Empty unless the add-on advertised a capability list, since an
        integration cannot be shown to be off by a list that was never sent.

    """
    if handshake is None or not handshake.capabilities:
        return frozenset()
    return frozenset(provider for provider in INTEGRATIONS if not advertises(handshake, provider))


def provider_of(tool_name: str) -> Provider | None:
    """
    Name the integration a tool belongs to.

    Args:
        tool_name: A tool name.

    Returns:
        Provider | None: The integration that gates it, or None for an ungated tool.

    """
    return next(
        (provider for provider, spec in INTEGRATIONS.items() if tool_name in module_tool_names(spec.tools_module)),
        None,
    )


def withheld_tools(handshake: AddonHandshake | None) -> frozenset[str]:
    """
    Name every tool a handshake's disabled integrations withhold.

    Args:
        handshake: The latest handshake, or None when there has been none.

    Returns:
        frozenset[str]: The tool names, mounted or not.

    """
    return frozenset().union(
        *(module_tool_names(INTEGRATIONS[provider].tools_module) for provider in withheld_providers(handshake))
    )


def disabled_note(provider: Provider) -> str:
    """
    Say that an integration is off and how to turn it on.

    Args:
        provider: The disabled integration.

    Returns:
        str: Two sentences: the checkbox that enables it, and where to read why it is off.

    """
    spec = INTEGRATIONS[provider]
    return (
        f"The {spec.label} integration is disabled in Blender for the open .blend: tick '{spec.checkbox}' in the "
        "3D Viewport sidebar (N) > BlenderMCP panel, then call get_addon_status, which re-reads it and returns "
        f'its tools to the tool list. get_integration_status(provider="{provider}") says why it is off.'
    )


def integration_refusal(tool_name: str, provider: Provider) -> str | None:
    """
    Refuse a gated tool whose integration is disabled, before it dispatches anything.

    Blocking: it may run a handshake round trip. A cached handshake that says the integration
    is off is confirmed with a fresh one before refusing, because capabilities are re-read only
    when the open file changes and the checkbox may have been ticked since. With no handshake
    yet, the one taken here is what decides.

    Args:
        tool_name: The tool about to run.
        provider: The integration that gates it, from `provider_of`.

    Returns:
        str | None: The refusal, or None to let the tool run.

    """
    handshake = get_last_handshake()
    if handshake is not None and provider not in withheld_providers(handshake):
        return None
    try:
        handshake = force_addon_handshake(get_blender_connection())
    except Exception as exc:
        # Blender cannot be reached; the tool's own round trip reports that, with the retry
        # remedy every tool gives, so this check adds no second transport error of its own.
        logger.debug(f"Integration check for {tool_name} skipped: {exc}")
        return None
    if provider not in withheld_providers(handshake):
        return None
    return f"'{tool_name}' was not sent to Blender. {disabled_note(provider)}"
