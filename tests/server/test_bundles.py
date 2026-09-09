"""Regression coverage for BLENDER_MCP_TOOLSETS bundle selection."""

import importlib
import subprocess
import sys

import pytest

from blender_mcp.server.bundles import ALL_MODULES, CORE_MODULES, resolve_toolset_modules


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (None, CORE_MODULES),
        ("", CORE_MODULES),
        ("  ", CORE_MODULES),
        ("cloth", (*CORE_MODULES, "cloth")),
        ("cloth,liquid", (*CORE_MODULES, "cloth", "liquid")),
        (" cloth , liquid ", (*CORE_MODULES, "cloth", "liquid")),
        ("cloth,cloth", (*CORE_MODULES, "cloth")),
        ("rigid-body", (*CORE_MODULES, "rigid_body", "scene_physics")),
        ("all", ALL_MODULES),
        ("ALL", ALL_MODULES),
    ],
)
def test_resolve_toolset_modules(raw_value: str | None, expected: tuple[str, ...]) -> None:
    """Every documented BLENDER_MCP_TOOLSETS value resolves to the modules it should import."""
    assert resolve_toolset_modules(raw_value) == expected


def test_resolve_toolset_modules_rejects_unknown_bundle() -> None:
    """An unrecognized bundle name fails loudly instead of silently registering nothing extra."""
    with pytest.raises(ValueError, match="Unknown BLENDER_MCP_TOOLSETS bundle"):
        resolve_toolset_modules("not-a-real-bundle")


def test_every_bundle_module_is_a_real_tools_submodule() -> None:
    """Every module named in BUNDLES/CORE_MODULES actually exists under blender_mcp.server.tools."""
    for module_name in ALL_MODULES:
        importlib.import_module(f"blender_mcp.server.tools.{module_name}")


def _tool_count_for_toolsets(raw_value: str | None) -> int:
    env_assignment = f"os.environ['BLENDER_MCP_TOOLSETS'] = {raw_value!r}\n" if raw_value is not None else ""
    script = (
        "import asyncio, os\n"
        f"{env_assignment}"
        "from blender_mcp.server import mcp\n"
        "print(len(asyncio.run(mcp.list_tools())))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


def test_unset_toolsets_registers_only_core_bundle() -> None:
    """A server started without BLENDER_MCP_TOOLSETS registers only the always-on core tools."""
    core_only_count = _tool_count_for_toolsets(None)
    everything_count = _tool_count_for_toolsets("all")

    assert 0 < core_only_count < everything_count


def test_selecting_a_bundle_adds_its_tools_on_top_of_core() -> None:
    """Selecting one bundle registers strictly more tools than core alone, and fewer than 'all'."""
    core_only_count = _tool_count_for_toolsets(None)
    with_cloth_count = _tool_count_for_toolsets("cloth")
    everything_count = _tool_count_for_toolsets("all")

    assert core_only_count < with_cloth_count < everything_count
