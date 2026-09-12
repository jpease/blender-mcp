"""Regression coverage for BLENDER_MCP_TOOLSETS bundle selection."""

import importlib
import json
import subprocess
import sys

import pytest

from blender_mcp.server.bundles import ALL_MODULES, CORE_MODULES, resolve_toolset_modules

_CORE_TODAY = (
    "core",
    "scene",
    "mesh",
    "model",
    "object_animation",
    "viewport",
    "animation",
)


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (None, _CORE_TODAY),
        ("", _CORE_TODAY),
        ("  ", _CORE_TODAY),
        ("cloth", (*_CORE_TODAY, "cloth")),
        ("cloth,liquid", (*_CORE_TODAY, "cloth", "liquid")),
        (" cloth , liquid ", (*_CORE_TODAY, "cloth", "liquid")),
        ("cloth,cloth", (*_CORE_TODAY, "cloth")),
        ("rigid-body", (*_CORE_TODAY, "rigid_body", "scene_physics")),
    ],
)
def test_resolve_toolset_modules(raw_value: str | None, expected: tuple[str, ...]) -> None:
    """
    Every documented BLENDER_MCP_TOOLSETS value resolves to the modules it should import.

    The expected tuples are spelled out rather than referencing CORE_MODULES, so that a change
    to the core set fails this test instead of silently redefining what it asserts.
    """
    assert resolve_toolset_modules(raw_value) == expected


def test_core_modules_matches_the_documented_core_set() -> None:
    """CORE_MODULES is what the parametrized cases above assume it is."""
    assert CORE_MODULES == _CORE_TODAY


@pytest.mark.parametrize("raw_value", ["all", "ALL"])
def test_all_sentinel_selects_every_module(raw_value: str) -> None:
    """The `all` sentinel resolves to every module, in a stable order, with no duplicates."""
    resolved = resolve_toolset_modules(raw_value)
    assert resolved == ALL_MODULES
    assert len(resolved) == len(set(resolved))


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


def _tool_names_for_toolsets(raw_value: str | None) -> set[str]:
    """
    Tool names a server process registers for a given BLENDER_MCP_TOOLSETS value.

    Args:
        raw_value: The BLENDER_MCP_TOOLSETS value to set, or None to leave it unset.

    Returns:
        The set of tool names that a fresh server process advertises for that selection.

    """
    env_assignment = f"os.environ['BLENDER_MCP_TOOLSETS'] = {raw_value!r}\n" if raw_value is not None else ""
    script = (
        "import asyncio, os, json\n"
        f"{env_assignment}"
        "from blender_mcp.server import mcp\n"
        "print(json.dumps([t.name for t in asyncio.run(mcp.list_tools())]))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    return set(json.loads(result.stdout))


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


def test_scene_authoring_tools_are_not_in_the_core_surface() -> None:
    """Geometry creation and destructive scene ops must not ship in every process."""
    core_tools = _tool_names_for_toolsets(None)
    for name in ("create_geometry_object", "reset_scene", "remove_scene_objects"):
        assert name not in core_tools, f"{name} is still registered by the core surface"


def test_scene_authoring_bundle_restores_them() -> None:
    """No capability is lost -- the same tools are reachable by asking for the bundle."""
    authoring_tools = _tool_names_for_toolsets("scene-authoring")
    for name in ("create_geometry_object", "reset_scene", "remove_scene_objects"):
        assert name in authoring_tools
