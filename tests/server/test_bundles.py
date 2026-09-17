"""Regression coverage for BLENDER_MCP_TOOLSETS bundle selection."""

import functools
import importlib.util
import json
import os
import re
import subprocess
import sys

from collections.abc import Iterable

import pytest

from conftest import REPO_ROOT

from blender_mcp.server import bundles
from blender_mcp.server.bundles import (
    ALL_MODULES,
    ALL_SENTINEL,
    BUNDLES,
    CORE_MODULES,
    MODES,
    TOOLSETS_ENV_VAR,
    resolve_toolset_modules,
)

# Safe despite this file's "nothing may import a tool module in-process" rule (see
# test_every_bundle_module_is_a_real_tools_submodule's docstring): under the lazy PEP 562 design
# (tools/_lazy_package.py), importing the bare camera/lighting *package* registers no tools --
# only importing one of their submodules does, which neither of these does.
from blender_mcp.server.tools import camera, lighting

_CORE_TODAY = (
    "core",
    "scene",
    "object_animation",
    "viewport",
    "animation",
    "file_lifecycle",
)

# Tools deliberately absent from the core surface and reachable only via `scene-authoring`.
_SCENE_AUTHORING_TOOLS = ("create_geometry_object", "reset_scene", "remove_scene_objects")

# Phase 2 Task 9's ten tools, one per addon file-lifecycle/linking command. `file_lifecycle`
# lives in CORE_MODULES (bundles.py ruling 1), so these are reachable from every mode/bundle
# selection, not just `shot`/`asset` -- but `shot` and `asset` are the two modes the ruling
# is about, so reachability is asserted against them specifically.
_FILE_LIFECYCLE_TOOLS = (
    "get_session_info",
    "open_shot",
    "save_shot",
    "reset_session",
    "link_canon_library",
    "create_override",
    "list_libraries",
    "reload_library",
    "relocate_library",
    "unlink_libraries",
)

# name -> (destructive, read_only, open_world), each checked by name in both directions
# (plan Task 9 Step 4b / acceptance criteria 3 and 3a). `_is_destructive`/`_is_read_only`/
# `openWorldHint=` in `_documentation.py` are the source of truth this table is checked
# against; re-derive it from there rather than from this table if the two ever disagree.
_FILE_LIFECYCLE_HINTS: dict[str, tuple[bool, bool, bool]] = {
    "get_session_info": (False, True, False),
    "open_shot": (True, False, True),
    "save_shot": (True, False, True),
    "reset_session": (True, False, False),
    "link_canon_library": (False, False, True),
    "create_override": (False, False, False),
    "list_libraries": (False, True, False),
    "reload_library": (True, False, True),
    "relocate_library": (True, False, True),
    "unlink_libraries": (True, False, False),
}


def _assert_resolves_exactly_to(resolved: tuple[str, ...], expected_modules: set[str]) -> None:
    """
    Assert a resolved module tuple is core-first, matches an expected module set, and is unique.

    Shared by every test that recomputes its expectation independently of
    `resolve_toolset_modules`'s own `_ordered_unique(CORE_MODULES + ...)` construction, so a bug
    in that exact expansion cannot reproduce identically in both the code and the test. Checks
    against `_CORE_TODAY`, not `CORE_MODULES`, for the same reason: a change to `CORE_MODULES`
    must fail a test here rather than silently move what the test expects.

    Args:
        resolved: The tuple returned by `resolve_toolset_modules`.
        expected_modules: The module set it should contain, independently derived.

    """
    assert resolved[: len(_CORE_TODAY)] == _CORE_TODAY
    assert set(resolved) == expected_modules
    assert len(resolved) == len(set(resolved))


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
        # `camera.inspection` is asserted here deliberately: the plan's own Task 5 brief omits it
        # from the bundle, which would orphan the module and drop `all` below 285.
        (
            "camera",
            (*_CORE_TODAY, "camera.animation", "camera.core", "camera.inspection", "camera.shots", "camera.targeting"),
        ),
        ("camera-rigs", (*_CORE_TODAY, "camera.rigs")),
        ("lighting", (*_CORE_TODAY, "lighting.environment", "lighting.inspection", "lighting.rendering")),
        # `lighting.rendering` too, not just `lighting.construction`: `create_studio_lighting`
        # calls `render_lighting_preview` directly, so the bundle cannot work without it -- see
        # the comment on `BUNDLES["lighting-construction"]` in bundles.py.
        ("lighting-construction", (*_CORE_TODAY, "lighting.construction", "lighting.rendering")),
        ("texture", (*_CORE_TODAY, "texture")),
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


def test_core_authoring_bundle_restores_mesh_and_model() -> None:
    """Mesh and model leave the unconditional core but stay reachable."""
    assert resolve_toolset_modules("core-authoring") == (*_CORE_TODAY, "mesh", "model")


def test_mode_names_resolve_to_their_bundle_sets() -> None:
    """A mode is one word that expands to a curated bundle list -- see `_assert_resolves_exactly_to`."""
    for mode, bundle_names in MODES.items():
        expected_modules = set(_CORE_TODAY) | {m for b in bundle_names for m in BUNDLES[b]}
        _assert_resolves_exactly_to(resolve_toolset_modules(mode), expected_modules)


@pytest.mark.parametrize(
    ("mutated_modes", "match"),
    [
        ({**MODES, "cloth": ("camera",)}, "shadow a bundle name"),
        ({**MODES, "ALL": ("camera",)}, "shadow the 'all' sentinel"),
        ({**MODES, "broken": ("not-a-real-bundle",)}, "do not exist in BUNDLES"),
        ({**MODES, "shot": ("camera", "camera", "rendering")}, "repeat a bundle name"),
    ],
)
def test_check_modes_are_well_formed_rejects_each_invariant_violation(
    monkeypatch: pytest.MonkeyPatch, mutated_modes: dict[str, tuple[str, ...]], match: str
) -> None:
    """
    `_check_modes_are_well_formed` (run at import time) must catch each way MODES can go bad.

    Exercises the validator directly against a mutated MODES rather than asserting today's MODES
    is valid: a real collision would already fail every test in this file at collection time (the
    check runs on import), so a same-data assertion could never independently fail. This test
    never calls `resolve_toolset_modules` -- the "ALL" case triggers on
    `_check_modes_are_well_formed`'s own case-insensitive sentinel check, a separate match against
    `ALL_SENTINEL` than the one `resolve_toolset_modules` runs against user-supplied input.
    """
    monkeypatch.setattr(bundles, "MODES", mutated_modes)
    with pytest.raises(ValueError, match=match):
        bundles._check_modes_are_well_formed()


def test_modes_are_composable_with_bundles() -> None:
    """An artist in shot mode who needs one extra domain must not have to abandon the mode."""
    resolved = resolve_toolset_modules("shot,retopology")
    assert set(resolve_toolset_modules("shot")) < set(resolved)


def _dotted_submodule_names(*bundle_names: str) -> set[str]:
    """
    Bare submodule names (the part after the dot) named by one or more `BUNDLES` entries.

    Args:
        bundle_names: Bundle names in `BUNDLES` whose dotted entries to collect.

    Returns:
        Each entry's text after its first `.`.

    """
    return {name.split(".", 1)[1] for bundle_name in bundle_names for name in BUNDLES[bundle_name]}


def test_camera_lazy_attribute_submodules_match_the_camera_bundles() -> None:
    """
    `camera.__getattr__`'s search set must track `BUNDLES`' `camera`/`camera-rigs` entries.

    The two lists serve different mechanisms -- production module import (`bundles.py`) vs.
    lazy package-attribute resolution (`camera/__init__.py`'s `_SUBMODULES`) -- so they cannot
    be the same object, but nothing else ties them together: a submodule added to one and not
    the other would silently be unreachable through that path. Importing the bare `camera`
    package (not a submodule) registers no tools under the lazy design, so this does not violate
    this file's "nothing may import a tool module in-process" rule -- verified by running this
    test alongside `test_selecting_a_bundle_adds_exactly_that_bundle_on_top_of_core` in the same
    session with no interference.
    """
    assert set(camera._SUBMODULES) == _dotted_submodule_names("camera", "camera-rigs")


def test_lighting_lazy_attribute_submodules_match_the_lighting_bundles() -> None:
    """Lighting equivalent of `test_camera_lazy_attribute_submodules_match_the_camera_bundles`."""
    assert set(lighting._SUBMODULES) == _dotted_submodule_names("lighting", "lighting-construction")


def test_camera_attribute_access_does_not_leak_the_excluded_rig_submodule() -> None:
    """
    Looking up an ordinary in-bundle `camera.<name>` must not import `camera.rigs` as a side effect.

    Regression for a bug the Task 5 cycle-1 architecture critic found: the first `lazy_getattr`
    imported each `_SUBMODULES` entry in turn, checking each with `hasattr`, until one defined
    the requested name -- and importing a submodule to check it is itself a side effect that
    registers its tools. With `rigs` listed before `targeting`, resolving
    `camera.create_camera_target` (a `targeting` name) used to import -- and register -- `rigs`
    along the way. Now fixed structurally (`lazy_getattr` parses source instead of importing to
    check), not just by reordering; this test still guards the property either fix must hold.
    """
    tools = _tool_names_for_toolsets(
        "camera", prelude="from blender_mcp.server.tools import camera\ncamera.create_camera_target\n"
    )
    assert "create_orbit_camera_rig" not in tools, "attribute access on an in-bundle name leaked camera.rigs"


def test_lighting_attribute_access_does_not_leak_the_excluded_construction_submodule() -> None:
    """Lighting equivalent of `test_camera_attribute_access_does_not_leak_the_excluded_rig_submodule`."""
    tools = _tool_names_for_toolsets(
        "lighting", prelude="from blender_mcp.server.tools import lighting\nlighting.render_lighting_preview\n"
    )
    assert "create_studio_lighting" not in tools, "attribute access on an in-bundle name leaked lighting.construction"


def test_camera_dir_does_not_leak_the_excluded_rig_submodule() -> None:
    """
    `dir(camera)` must not import `camera.rigs` as a side effect.

    Regression for a bug the Task 5 cycle-2 architecture critic found: the first fix (reordering
    `_SUBMODULES`) only protected `lazy_getattr`'s linear scan; `lazy_dir` unconditionally
    imported every entry in `_SUBMODULES` regardless of order, so a plain `dir(camera)` call --
    an ordinary IDE-completion/debugger operation, not an edge case -- always registered
    `rigs`'s tools. Fixed structurally: `lazy_dir` now lists names by parsing submodule source,
    never by importing.
    """
    tools = _tool_names_for_toolsets("camera", prelude="from blender_mcp.server.tools import camera\ndir(camera)\n")
    assert "create_orbit_camera_rig" not in tools, "dir(camera) leaked camera.rigs"


def test_lighting_dir_does_not_leak_the_excluded_construction_submodule() -> None:
    """Lighting equivalent of `test_camera_dir_does_not_leak_the_excluded_rig_submodule`."""
    tools = _tool_names_for_toolsets(
        "lighting", prelude="from blender_mcp.server.tools import lighting\ndir(lighting)\n"
    )
    assert "create_studio_lighting" not in tools, "dir(lighting) leaked lighting.construction"


def test_camera_missing_attribute_does_not_leak_the_excluded_rig_submodule() -> None:
    """
    A nonexistent `camera.<name>` lookup must not import `camera.rigs` before raising.

    Regression for the second bug the Task 5 cycle-2 architecture critic found: the reordering
    fix only protects names that resolve inside an in-bundle submodule -- a name that exists
    nowhere still walked the whole `_SUBMODULES` tuple, including `rigs`, before raising
    `AttributeError`. Fixed structurally: `lazy_getattr` now checks a submodule's *parsed* name
    set before importing it, so a name that exists nowhere in `_SUBMODULES` never triggers an
    import at all.
    """
    tools = _tool_names_for_toolsets(
        "camera",
        prelude=(
            "from blender_mcp.server.tools import camera\n"
            "try:\n"
            "    camera.this_name_does_not_exist_anywhere\n"
            "except AttributeError:\n"
            "    pass\n"
        ),
    )
    assert "create_orbit_camera_rig" not in tools, "a missing-name lookup leaked camera.rigs"


def test_lighting_missing_attribute_does_not_leak_the_excluded_construction_submodule() -> None:
    """Lighting equivalent of `test_camera_missing_attribute_does_not_leak_the_excluded_rig_submodule`."""
    tools = _tool_names_for_toolsets(
        "lighting",
        prelude=(
            "from blender_mcp.server.tools import lighting\n"
            "try:\n"
            "    lighting.this_name_does_not_exist_anywhere\n"
            "except AttributeError:\n"
            "    pass\n"
        ),
    )
    assert "create_studio_lighting" not in tools, "a missing-name lookup leaked lighting.construction"


def test_camera_shared_names_resolve_via_the_in_bundle_submodule_not_rigs() -> None:
    """
    `camera.FollowForwardAxis`/`camera.UpAxis` must resolve without importing `camera.rigs`.

    Regression for a false claim the Task 5 cycle-3 comment critic caught in `camera/__init__.py`'s
    `_SUBMODULES` comment: both names are defined in `_shared.py` and re-exported by *both*
    `targeting.py` and `rigs.py`, so `_SUBMODULES` order is not a no-op tie-break here -- with
    `targeting` checked before `rigs`, these names resolve via `targeting` and never import
    `rigs`; reversing the order was verified (by hand) to leak `rigs` to get the identical value.
    This test pins the current, correct order's behavior.
    """
    tools = _tool_names_for_toolsets(
        "camera",
        prelude=("from blender_mcp.server.tools import camera\ncamera.FollowForwardAxis\ncamera.UpAxis\n"),
    )
    assert "create_orbit_camera_rig" not in tools, "resolving a name shared with rigs still leaked rigs"


def test_ast_derived_submodule_names_match_the_real_runtime_attributes() -> None:
    """
    `_submodule_top_level_names`'s static parse must agree with each submodule's real `vars()`.

    Turns the one-off manual check recorded in `_lazy_package.py`'s module docstring into a
    standing regression test, per the Task 5 cycle-3 architecture critic: a future submodule
    using a binding form the AST scan doesn't handle (tuple-unpacking, a module-level
    `if`/`try`/`for`/`with`) would silently under-report names, causing a false `AttributeError`
    for that name rather than a leak -- lower severity than the leaks cycles 1-2 found, but
    still worth catching before it ships.

    Run in a subprocess per this file's rule that nothing here may import a tool module
    in-process: this test imports every camera/lighting submodule to read back its real
    `vars()`, which would otherwise leave every later test looking at the full catalog. The
    submodule lists themselves come from the live `camera`/`lighting` packages imported at the
    top of this file (safe -- importing the bare package registers no tools, see the comment
    there), not retyped, so a submodule added to `_SUBMODULES` is covered here automatically
    instead of this test silently stopping short of it.
    """
    packages = {
        "blender_mcp.server.tools.camera": camera._SUBMODULES,
        "blender_mcp.server.tools.lighting": lighting._SUBMODULES,
    }
    script = (
        "import importlib, json\n"
        "from blender_mcp.server.tools._lazy_package import _submodule_top_level_names\n"
        f"packages = {packages!r}\n"
        "mismatches = {}\n"
        "for package_name, submodules in packages.items():\n"
        "    package = importlib.import_module(package_name)\n"
        "    for submodule_name in submodules:\n"
        "        submodule = importlib.import_module(f'{package_name}.{submodule_name}')\n"
        "        runtime = {n for n in vars(submodule) if not n.startswith('__')}\n"
        "        static = set(_submodule_top_level_names(package.__file__, submodule_name))\n"
        "        if runtime != static:\n"
        "            mismatches[f'{package_name}.{submodule_name}'] = {\n"
        "                'missing_in_static': sorted(runtime - static),\n"
        "                'extra_in_static': sorted(static - runtime),\n"
        "            }\n"
        "print(json.dumps(mismatches))\n"
    )
    mismatches = json.loads(_run_server_script(None, script))
    assert not mismatches, f"AST-derived names disagree with runtime for: {mismatches}"


def test_texture_lighting_alias_resolves_to_the_full_pre_split_surface() -> None:
    """
    The retired fused bundle name stays usable, losing no capability for an existing config.

    Recomputed from the split bundles' own module tuples rather than hand-listed, so the alias
    cannot silently drift from the sum of the parts it replaces.
    """
    expected = set(BUNDLES["texture"]) | set(BUNDLES["lighting"]) | set(BUNDLES["lighting-construction"])
    assert set(resolve_toolset_modules("texture-lighting")) - set(resolve_toolset_modules(None)) == expected


def test_shot_and_asset_modes_share_only_the_core_surface() -> None:
    """Selecting shot must not drag in asset authoring, and vice versa."""
    core = set(_tool_names_for_toolsets(None))
    shot = set(_tool_names_for_toolsets("shot")) - core
    asset = set(_tool_names_for_toolsets("asset")) - core

    assert shot and asset, "each mode must add tools of its own"
    assert shot.isdisjoint(asset), f"modes overlap outside core: {sorted(shot & asset)}"


def test_shot_mode_excludes_texture_authoring_and_light_or_rig_construction() -> None:
    """
    Shot excludes texture authoring, rig construction and light construction.

    Lighting and camera must be selectable without dragging in texture authoring or the
    construction workflows a shot rarely reaches -- those stay behind `texture`, `camera-rigs`
    and `lighting-construction`, opt-in bundles a shot can still compose with if it needs them.

    Deviates from the plan's own Step 2 snippet (2026-09-11-phase-1-catalog.md:671-676), which
    asserts `create_studio_lighting` (defined in `lighting/construction.py`) belongs to `shot`.
    That contradicts the same plan's Interfaces line ("lighting-construction" is its own bundle)
    and the measured 53-tool target in PHASE1_TASK_STATE.md, which is rung 3 of the spec's own
    ladder -- explicitly "minus lighting.construction". Verified against Step 1's real submodule
    listing before writing this assertion, per the plan's own instruction to do so.
    """
    shot = _tool_names_for_toolsets("shot")
    assert "configure_hdri_environment" in shot, "lighting (minus construction) must still be in shot"
    assert not {t for t in shot if t.startswith(("create_pbr_material", "apply_pbr_texture_set"))}, (
        "texture authoring must not ride along with shot"
    )
    assert "create_studio_lighting" not in shot, "light construction belongs to lighting-construction, not shot"
    assert "create_orbit_camera_rig" not in shot, "rig construction belongs to camera-rigs, not shot"


@pytest.mark.parametrize("raw_value", ["all", "ALL"])
def test_all_sentinel_selects_every_module(raw_value: str) -> None:
    """
    The `all` sentinel resolves to core plus every bundle, core first, with no duplicates.

    The expectation is recomputed from BUNDLES -- the authored source of truth -- rather than
    compared against ALL_MODULES, which is what the function under test returns. Asserting
    against ALL_MODULES would pass for any bug in how ALL_MODULES itself is built.
    """
    every_bundle_module = {module for modules in BUNDLES.values() for module in modules}
    _assert_resolves_exactly_to(resolve_toolset_modules(raw_value), set(_CORE_TODAY) | every_bundle_module)


def test_resolve_toolset_modules_rejects_unknown_bundle() -> None:
    """An unrecognized name fails loudly instead of silently registering nothing extra."""
    with pytest.raises(ValueError, match="Unknown BLENDER_MCP_TOOLSETS name"):
        resolve_toolset_modules("not-a-real-bundle")


def test_resolve_toolset_modules_error_lists_modes_separately_from_bundles() -> None:
    """An artist typing `shots` should be told the modes are `shot`/`asset`, not every bundle."""
    with pytest.raises(ValueError, match=r"Available modes: asset, shot\. Available bundles: "):
        resolve_toolset_modules("shots")


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


def _run_server_script(raw_value: str | None, script: str) -> str:
    """
    Run `script` in a fresh subprocess with BLENDER_MCP_TOOLSETS set to `raw_value`.

    The one place every "measure a real server process's selection" helper below builds its
    environment and invokes Python, so the three of them cannot drift into three different
    isolation strategies. The selection is passed through an explicit, stripped-and-rebuilt
    environment rather than inherited, so a developer who exports BLENDER_MCP_TOOLSETS in their
    own shell cannot change what these tests measure. `None` means the variable is genuinely
    absent from the child process. `script` is responsible for its own imports (after this
    function has set the environment) and for printing whatever its caller will parse back out.

    Args:
        raw_value: The BLENDER_MCP_TOOLSETS value to set, or None to leave it unset.
        script: The child process's `python -c` source.

    Returns:
        The child process's captured stdout.

    """
    env = {key: value for key, value in os.environ.items() if key != TOOLSETS_ENV_VAR}
    if raw_value is not None:
        env[TOOLSETS_ENV_VAR] = raw_value
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True, env=env)
    return result.stdout


@functools.cache
def _tool_names_for_toolsets(raw_value: str | None, prelude: str = "") -> frozenset[str]:
    """
    Tool names a server process registers for a given BLENDER_MCP_TOOLSETS value.

    Cached: each selection costs a full server import, and several tests ask for the same ones.

    Args:
        raw_value: The BLENDER_MCP_TOOLSETS value to set, or None to leave it unset.
        prelude: Extra statements run after `mcp` is imported but before the tool list is
            captured -- e.g. an attribute access or a `dir()` call whose side effects on the
            registered tool set a test wants to observe.

    Returns:
        The set of tool names that a fresh server process advertises for that selection, after
        `prelude` has run.

    """
    script = (
        "import asyncio, json\n"
        "from blender_mcp.server import mcp\n"
        f"{prelude}"
        "print(json.dumps([t.name for t in asyncio.run(mcp.list_tools())]))\n"
    )
    return frozenset(json.loads(_run_server_script(raw_value, script)))


def _payload_bytes_for_toolsets(raw_value: str | None) -> int:
    """
    Total advertised payload bytes a server process registers for a given BLENDER_MCP_TOOLSETS value.

    Not cached, unlike `_tool_names_for_toolsets`: this file has exactly one call site for it
    today (the shot-mode ceiling test below), the same reason `_tool_annotations_for_toolsets`
    isn't cached either -- add `@functools.cache` back if a second caller shows up. Uses
    `payload_report(...).total_bytes` rather than the removed `payload_bytes`/`tool_bytes`
    functions, which Task 5's plan explicitly names as dead second paths to this same number --
    see `catalog_metrics.py`.

    Args:
        raw_value: The BLENDER_MCP_TOOLSETS value to set, or None to leave it unset.

    Returns:
        `payload_report(...).total_bytes` for that selection's advertised tools.

    """
    script = (
        "import asyncio\n"
        "from blender_mcp.server import mcp\n"
        "from blender_mcp.server.catalog_metrics import payload_report\n"
        "print(payload_report(asyncio.run(mcp.list_tools())).total_bytes)\n"
    )
    return int(_run_server_script(raw_value, script).strip())


# Measured 2026-09-14 at 547ba0a (dirty; Phase 1 Tasks 1-5 uncommitted), after Task 5's camera/
# lighting splits, then raised once, deliberately, at Phase 2 Task 9: `file_lifecycle` (ten
# tools, one per addon file-lifecycle/linking command) joined CORE_MODULES per bundles.py
# ruling 1, adding 14,625 B to `shot` (measured before: 203,093 B; after: 217,718 B, after the
# cycle-1 repair round's prose trims) -- under ruling 3's 15,000 B budget for the ten tools.
# This is a ceiling, not a target -- the policy is to minimize, so this number should only ever
# move down from here. Raising it again requires a deliberate decision recorded in the commit
# message. See docs/superpowers/plans/PHASE1_TASK_STATE.md's "Task 6" section for how it was
# first measured, and PHASE2_TASK_STATE.md for this raise. Raised again by 343 B after Phase 2
# (measured 217,718 -> 218,061): `save_shot.create_directories`, so a canon/shots layout needs
# no pre-created directories, and `get_object_info`'s measured `library`/`is_override` fields,
# which say which object a name shared by an override and its linked original resolved to.
# 218,061, not the 218,068 first committed: the critic cycle's wording repairs came in 7 B
# cheaper, and a ceiling only ever moves down.
SHOT_MODE_BYTE_CEILING = 218_061

# Added at Phase 2 Task 9 alongside the `shot` raise above: core is now where the file-lifecycle
# growth lands, and it was previously pinned by nothing. Measured the same way, same commit:
# default/core was 63,394 B before Task 9, 78,019 B after -- the same 14,625 B delta, since the
# ten tools are core-only and add nothing to `shot` beyond what core already carries there.
# Raised by the same 343 B as `shot` after Phase 2 (78,019 -> 78,362), for the same two core tools.
DEFAULT_MODE_BYTE_CEILING = 78_362


def test_shot_mode_payload_stays_under_its_ceiling() -> None:
    """
    The shot-assembly surface must not grow back. Lower is always acceptable.

    Measures the `shot` mode by name, not by its current bundle expansion spelled out as a
    literal string: if `MODES["shot"]` is ever edited, this test must keep measuring whatever
    `shot` actually resolves to, not a stale copy of today's tuple.
    """
    assert _payload_bytes_for_toolsets("shot") <= SHOT_MODE_BYTE_CEILING


def test_default_mode_payload_stays_under_its_ceiling() -> None:
    """The always-on core surface must not grow back either. Lower is always acceptable."""
    assert _payload_bytes_for_toolsets(None) <= DEFAULT_MODE_BYTE_CEILING


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


@functools.cache
def _readme_text() -> str:
    """
    Read README.md once and share it across every drift test in this file.

    Returns:
        README.md's full text.

    """
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def _names_missing_from_readme(names: Iterable[str]) -> list[str]:
    """
    Names not rendered as `` `name` `` anywhere in README.

    One definition shared by every test that checks a set of code-defined names against what
    README documents, so the "is `name` backtick-documented?" rule cannot drift between them.

    Args:
        names: Names to look for.

    Returns:
        The subset of `names` not found in README, sorted.

    """
    readme = _readme_text()
    return sorted(name for name in names if f"`{name}`" not in readme)


def test_readme_documents_every_bundle_name() -> None:
    """README's bundle table is what users read to learn the names, so it must track BUNDLES."""
    missing = _names_missing_from_readme(BUNDLES)
    assert not missing, f"bundles missing from README's table: {missing}"


def test_readme_documents_every_mode_name() -> None:
    """
    README's mode table must track MODES, the same way its bundle table tracks BUNDLES.

    Only mode *names* are checked here: every bundle a mode can name is already covered by
    `test_readme_documents_every_bundle_name` (it checks all of `BUNDLES`, a superset of any
    one mode's bundle list) and is guaranteed to exist in `BUNDLES` at all by
    `_check_modes_are_well_formed`, which runs at import time - so a per-mode bundle check here
    could never independently fail.
    """
    missing_modes = _names_missing_from_readme(MODES)
    assert not missing_modes, f"modes missing from README's table: {missing_modes}"


def _tool_annotations_for_toolsets(raw_value: str | None) -> dict[str, dict[str, bool]]:
    """
    Destructive/read-only hints a server process advertises for a given selection.

    Measured in a subprocess for the same reason the name set is: only a process that imported
    the modules through `tools/__init__` has run `finalize_tool_documentation`, so annotations
    are absent from any tool imported late into the test process.

    Args:
        raw_value: The BLENDER_MCP_TOOLSETS value to set, or None to leave it unset.

    Returns:
        Tool name mapped to its `destructive`, `read_only` and `open_world` hints.

    """
    script = (
        "import asyncio, json\n"
        "from blender_mcp.server import mcp\n"
        "print(json.dumps({\n"
        "    t.name: {\n"
        '        "destructive": bool(t.annotations and t.annotations.destructiveHint),\n'
        '        "read_only": bool(t.annotations and t.annotations.readOnlyHint),\n'
        '        "open_world": bool(t.annotations and t.annotations.openWorldHint),\n'
        "    }\n"
        "    for t in asyncio.run(mcp.list_tools())\n"
        "}))\n"
    )
    return json.loads(_run_server_script(raw_value, script))


def test_scene_authoring_tools_advertise_their_destructiveness() -> None:
    """
    The tools moved out of core because they are destructive must say so on the wire.

    `destructiveHint` is what a client reads when deciding whether a call is safe, and it is
    the stated reason these tools are not in the default surface. `reset_scene` in particular
    matches no destructive name prefix, so only its entry in `_DESTRUCTIVE_TOOLS` marks it.
    """
    annotations = _tool_annotations_for_toolsets("scene-authoring")

    for name in ("reset_scene", "remove_scene_objects"):
        assert annotations[name]["destructive"], f"{name} is destructive but does not advertise it"
        assert not annotations[name]["read_only"], f"{name} mutates but advertises read_only"
    assert not annotations["create_geometry_object"]["destructive"], (
        "create_geometry_object adds an object; marking it destructive would devalue the hint"
    )


def test_file_lifecycle_tools_are_exactly_ten_and_reachable_from_shot_and_asset() -> None:
    """
    Phase 2 Task 9's ten tools exist once each and are reachable from both modes.

    `file_lifecycle` lives in CORE_MODULES (bundles.py ruling 1) precisely so `shot` and
    `asset` both reach it without a bundle shared by both modes, which
    `test_shot_and_asset_modes_share_only_the_core_surface` forbids. Checked against
    `_tool_names_for_toolsets`, not by reading bundles.py, per acceptance criterion 1.
    """
    assert len(_FILE_LIFECYCLE_TOOLS) == 10  # ruff: ignore[magic-value-comparison] - the count IS the assertion
    assert set(_FILE_LIFECYCLE_TOOLS) == set(_FILE_LIFECYCLE_HINTS)
    shot = _tool_names_for_toolsets("shot")
    asset = _tool_names_for_toolsets("asset")
    for name in _FILE_LIFECYCLE_TOOLS:
        assert name in shot, f"{name} is not reachable from the shot mode"
        assert name in asset, f"{name} is not reachable from the asset mode"


def test_file_lifecycle_tools_advertise_correct_hints() -> None:
    """
    All ten tools advertise the right destructive/read-only/open-world hints, by name, both ways.

    `finalize_tool_documentation` emits all three hints unconditionally on every tool
    (`_documentation.py:finalize_tool_documentation`), so a tool omitted from the right set
    does not ship "no hint" -- it ships an affirmatively wrong one. Thirty assertions: five
    tools must be destructive-and-open-world (`open_shot`, `save_shot`, `reload_library`,
    `relocate_library` -- plus `unlink_libraries`, destructive but NOT open-world), two must
    be read-only (`get_session_info`, `list_libraries`), `link_canon_library` must be
    open-world but NOT destructive, `create_override` must be neither, and `reset_session`
    must be destructive but NOT open-world (it reads nothing from disk).
    """
    annotations = _tool_annotations_for_toolsets("shot")
    for name, (destructive, read_only, open_world) in _FILE_LIFECYCLE_HINTS.items():
        actual = annotations[name]
        assert actual["destructive"] == destructive, f"{name}: expected destructive={destructive}"
        assert actual["read_only"] == read_only, f"{name}: expected read_only={read_only}"
        assert actual["open_world"] == open_world, f"{name}: expected open_world={open_world}"


# The exact sentence `_documentation.py`'s `_BLEND_FILE_TOOLS` branch emits (Task 9 repair
# round): reads or writes a .blend file on disk, with no per-tool detail folded in.
_BLEND_FILE_EFFECTS_SENTENCE = "reads or writes a .blend file on disk"

# The five tools that must carry that sentence and openWorldHint=True.
_BLEND_FILE_TOOLS_UNDER_TEST = ("open_shot", "save_shot", "link_canon_library", "reload_library", "relocate_library")


def _tool_descriptions_for_toolsets(raw_value: str | None) -> dict[str, str]:
    """
    Full tool descriptions a server process advertises for a given selection.

    Measured in a subprocess for the same reason `_tool_annotations_for_toolsets` is: only a
    process that imported the modules through `tools/__init__` has run
    `finalize_tool_documentation`, so a tool imported late into the test process carries the
    bare docstring, not the `_tool_contract` suffix this test checks.

    Args:
        raw_value: The BLENDER_MCP_TOOLSETS value to set, or None to leave it unset.

    Returns:
        Tool name mapped to its full advertised description.

    """
    script = (
        "import asyncio, json\n"
        "from blender_mcp.server import mcp\n"
        "print(json.dumps({t.name: t.description for t in asyncio.run(mcp.list_tools())}))\n"
    )
    return json.loads(_run_server_script(raw_value, script))


def test_file_lifecycle_tools_blend_file_prose_is_correct() -> None:
    """
    None of the ten claims it skips saving the .blend file; the five that touch one say so correctly.

    `_FILE_TOOLS`'s effects sentence ends "it does not save the .blend file" -- true of that
    set's own members, false for `save_shot` and misleading for `open_shot` and the three
    library commands. Folding the five `_BLEND_FILE_TOOLS` into `_FILE_TOOLS`, or deleting the
    `_BLEND_FILE_TOOLS` prose branch in `_tool_contract` (falling through to the generic
    "mutates connected Blender state but never saves the .blend file" branch), both pass every
    other Task 9 test and both are caught here.
    """
    descriptions = _tool_descriptions_for_toolsets("shot")
    for name in _FILE_LIFECYCLE_TOOLS:
        assert "does not save the .blend file" not in descriptions[name], (
            f"{name} carries the _FILE_TOOLS prose, which is false or misleading for it"
        )
    for name in _BLEND_FILE_TOOLS_UNDER_TEST:
        assert _BLEND_FILE_EFFECTS_SENTENCE in descriptions[name], f"{name} is missing the .blend-file effects sentence"
