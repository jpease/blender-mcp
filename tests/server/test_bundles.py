"""Regression coverage for BLENDER_MCP_TOOLSETS bundle selection."""

import functools
import importlib.util
import json
import os
import re
import subprocess
import sys

import pytest

from conftest import REPO_ROOT

from blender_mcp.server import bundles
from blender_mcp.server.bundles import (
    ALL_MODULES,
    ALL_SENTINEL,
    BUNDLES,
    CORE_MODULES,
    TOOLSETS_ENV_VAR,
    resolve_toolset_modules,
)

_CORE_TODAY = (
    "core",
    "scene",
    "mesh",
    "model",
    "object_animation",
    "viewport",
    "animation",
)

# Tools deliberately absent from the core surface and reachable only via `scene-authoring`.
_SCENE_AUTHORING_TOOLS = ("create_geometry_object", "reset_scene", "remove_scene_objects")


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
    """
    The `all` sentinel resolves to core plus every bundle, core first, with no duplicates.

    The expectation is recomputed from BUNDLES -- the authored source of truth -- rather than
    compared against ALL_MODULES, which is what the function under test returns. Asserting
    against ALL_MODULES would pass for any bug in how ALL_MODULES itself is built.
    """
    resolved = resolve_toolset_modules(raw_value)
    every_bundle_module = {module for modules in BUNDLES.values() for module in modules}

    assert resolved[: len(_CORE_TODAY)] == _CORE_TODAY
    assert set(resolved) == set(_CORE_TODAY) | every_bundle_module
    assert len(resolved) == len(set(resolved))


def test_resolve_toolset_modules_rejects_unknown_bundle() -> None:
    """An unrecognized bundle name fails loudly instead of silently registering nothing extra."""
    with pytest.raises(ValueError, match="Unknown BLENDER_MCP_TOOLSETS bundle"):
        resolve_toolset_modules("not-a-real-bundle")


def test_every_bundle_module_is_a_real_tools_submodule() -> None:
    """
    Every module named in BUNDLES/CORE_MODULES actually exists under blender_mcp.server.tools.

    Uses `find_spec` rather than importing: a tool module registers its tools onto a
    process-global FastMCP singleton at import time, so importing all of them here would
    leave every later test in this session looking at the full catalog regardless
    of the selection under test. Nothing in this file may import a tool module in-process.
    """
    for module_name in ALL_MODULES:
        assert importlib.util.find_spec(f"blender_mcp.server.tools.{module_name}") is not None, (
            f"{module_name} is named by a bundle but does not exist"
        )


@functools.cache
def _tool_names_for_toolsets(raw_value: str | None) -> frozenset[str]:
    """
    Tool names a server process registers for a given BLENDER_MCP_TOOLSETS value.

    Cached: each selection costs a full server import, and several tests ask for the same
    ones. The selection is passed through an explicit environment rather than inherited, so a
    developer who exports BLENDER_MCP_TOOLSETS in their own shell cannot change what these
    tests measure. `None` means the variable is genuinely absent from the child process.

    Args:
        raw_value: The BLENDER_MCP_TOOLSETS value to set, or None to leave it unset.

    Returns:
        The set of tool names that a fresh server process advertises for that selection.

    """
    env = {key: value for key, value in os.environ.items() if key != TOOLSETS_ENV_VAR}
    if raw_value is not None:
        env[TOOLSETS_ENV_VAR] = raw_value
    script = (
        "import asyncio, json\n"
        "from blender_mcp.server import mcp\n"
        "print(json.dumps([t.name for t in asyncio.run(mcp.list_tools())]))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True, env=env)
    return frozenset(json.loads(result.stdout))


def test_selecting_a_bundle_adds_exactly_that_bundle_on_top_of_core() -> None:
    """
    Selecting one bundle adds its own tools to core and nothing else.

    Asserted as a set relation rather than as `core < cloth < all` counts: an ordering of
    three integers holds even if the wrong tools were added, which is the regression that
    would actually matter here.
    """
    core_only = _tool_names_for_toolsets(None)
    with_cloth = _tool_names_for_toolsets("cloth")
    everything = _tool_names_for_toolsets(ALL_SENTINEL)

    assert core_only, "a default process must still advertise the core surface"
    assert core_only < with_cloth < everything, "each selection must be a strict superset of the last"
    assert with_cloth - core_only, "selecting cloth must add tools core does not already have"
    assert (with_cloth - core_only).isdisjoint(_tool_names_for_toolsets("retopology") - core_only)


def test_all_advertises_the_tool_count_quoted_in_bundles_docs() -> None:
    """
    The tool count quoted in bundles.py's module docstring is what `all` actually advertises.

    Parsed out of the docstring rather than restated here, so the prose and the measured
    catalog are pinned to each other and neither can rot alone. Scoped to this test: a
    module-level parse would turn a harmless rewording into a collection error for the file.
    """
    quoted = re.search(r"\((\d+) tools, per", bundles.__doc__ or "")
    assert quoted, "bundles.py's docstring no longer quotes a tool count in the expected form"
    assert len(_tool_names_for_toolsets(ALL_SENTINEL)) == int(quoted.group(1))


def test_scene_authoring_tools_are_not_in_the_core_surface() -> None:
    """Geometry creation and destructive scene ops must not ship in every process."""
    core_tools = _tool_names_for_toolsets(None)
    for name in _SCENE_AUTHORING_TOOLS:
        assert name not in core_tools, f"{name} is still registered by the core surface"


def test_scene_authoring_bundle_restores_them() -> None:
    """No capability is lost -- the same tools are reachable by asking for the bundle."""
    authoring_tools = _tool_names_for_toolsets("scene-authoring")
    for name in _SCENE_AUTHORING_TOOLS:
        assert name in authoring_tools, f"{name} is not reachable through the scene-authoring bundle"


def test_readme_documents_every_bundle_name() -> None:
    """README's bundle table is what users read to learn the names, so it must track BUNDLES."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    missing = sorted(name for name in BUNDLES if f"`{name}`" not in readme)
    assert not missing, f"bundles missing from README's table: {missing}"
