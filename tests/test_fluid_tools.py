"""Regression coverage for the canonical liquid/gas fluid facade."""

import asyncio

from conftest import load_addon

from blender_mcp.server.tools import _dispatch
from blender_mcp.server.tools import liquid as fluid


class _Connection:
    def __init__(self) -> None:
        self.calls = []

    def send_command(self, command, params):
        self.calls.append((command, params))
        return {"domain_type": params.get("domain_type"), "changed_objects": []}


def test_canonical_fluid_tools_are_registered_and_dispatched(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, data={})
    commands = {
        "inspect_fluid_simulation",
        "create_fluid_domain",
        "configure_fluid_solver",
        "add_fluid_flow",
        "add_fluid_effector",
        "manage_fluid_cache",
    }
    assert commands <= set(fluid.mcp._tool_manager._tools)
    assert commands <= set(addon.BlenderMCPServer()._build_command_handlers())
    assert addon.BlenderMCPServer.command_spec("inspect_fluid_simulation").read_only


def test_gas_solver_patch_uses_canonical_domain_discriminator(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(
        _dispatch, "send_command", lambda command, params=None: connection.send_command(command, params)
    )

    asyncio.run(
        fluid.configure_fluid_solver(
            ctx=None,
            domain_type="GAS",
            domain_object_name="Smoke Domain",
            modifier_name="Fluid Domain",
            patch=fluid.FluidSolverPatch(vorticity=1.5, use_noise=True, noise_scale=2),
        )
    )

    assert connection.calls[0][0] == "configure_fluid_solver"
    assert connection.calls[0][1]["domain_type"] == "GAS"
    assert connection.calls[0][1]["patch"] == {"vorticity": 1.5, "use_noise": True, "noise_scale": 2}
