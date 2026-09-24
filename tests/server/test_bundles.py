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
    resolve_toolset_bundles,
    resolve_toolset_modules,
)
from blender_mcp.server.mount_map import (
    CORE_BUNDLE,
    bundle_tool_names,
    bundles_providing,
    known_tool_names,
    unmounted_bundle_counts,
)

# Tool modules must not be imported in-process, but these lazy packages register no tools
# until a submodule is imported.
from blender_mcp.server.tools import camera, character_rigging, lighting

_CORE_TODAY = (
    "core",
    "scene",
    "object_animation",
    "viewport",
    "animation",
    "file_lifecycle",
)

# Reachable only through `scene-authoring`. `remove_scene_objects` is deliberately not here: a
# session that cannot delete the objects it created leaves them in the saved shot.
_SCENE_AUTHORING_TOOLS = ("create_geometry_object", "reset_scene")

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
    "inspect_delivery",
)

# name -> (destructive, read_only, open_world). If this disagrees with `_documentation.py`,
# re-derive the intended hints; do not just copy either side.
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
    "inspect_delivery": (False, True, True),
}


def _assert_resolves_exactly_to(resolved: tuple[str, ...], expected_modules: set[str]) -> None:
    """
    Assert a resolved module tuple is core-first, matches an expected module set, and is unique.

    Compares against `_CORE_TODAY`, not `CORE_MODULES`, so a change to core fails here instead of
    moving the expectation with it.

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
        (
            "camera",
            (*_CORE_TODAY, "camera.animation", "camera.core", "camera.inspection", "camera.shots", "camera.targeting"),
        ),
        ("camera-rigs", (*_CORE_TODAY, "camera.rigs")),
        ("lighting", (*_CORE_TODAY, "lighting.environment", "lighting.inspection", "lighting.rendering")),
        # `construction` imports `rendering`; see the comment in bundles.py.
        ("lighting-construction", (*_CORE_TODAY, "lighting.construction", "lighting.rendering")),
        ("texture", (*_CORE_TODAY, "texture")),
    ],
)
def test_resolve_toolset_modules(raw_value: str | None, expected: tuple[str, ...]) -> None:
    """Every documented BLENDER_MCP_TOOLSETS value resolves to the modules it should import."""
    assert resolve_toolset_modules(raw_value) == expected


def test_core_modules_matches_the_documented_core_set() -> None:
    """CORE_MODULES is what the parametrized cases above assume it is."""
    assert CORE_MODULES == _CORE_TODAY


def test_core_authoring_bundle_restores_mesh_and_model() -> None:
    """Mesh and model leave the unconditional core but stay reachable."""
    assert resolve_toolset_modules("core-authoring") == (*_CORE_TODAY, "mesh", "model")


def test_mode_names_resolve_to_their_bundle_sets() -> None:
    """A mode name resolves to core plus its bundles' modules."""
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
    `_check_modes_are_well_formed` catches each way MODES can go bad.

    Runs against a mutated MODES, because a bad real MODES already fails at import.
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
    Collect the submodule names after the dot in the given bundles' entries.

    Args:
        bundle_names: Bundle names in `BUNDLES` whose dotted entries to collect.

    Returns:
        Each entry's text after its first `.`.

    """
    return {name.split(".", 1)[1] for bundle_name in bundle_names for name in BUNDLES[bundle_name]}


def test_camera_lazy_attribute_submodules_match_the_camera_bundles() -> None:
    """
    `camera._SUBMODULES` must track the `camera` and `camera-rigs` bundles.

    Nothing else ties the two lists together; a submodule missing from one is silently
    unreachable through that path.
    """
    assert set(camera._SUBMODULES) == _dotted_submodule_names("camera", "camera-rigs")


def test_lighting_lazy_attribute_submodules_match_the_lighting_bundles() -> None:
    """Lighting equivalent of `test_camera_lazy_attribute_submodules_match_the_camera_bundles`."""
    assert set(lighting._SUBMODULES) == _dotted_submodule_names("lighting", "lighting-construction")


def test_camera_attribute_access_does_not_leak_the_excluded_rig_submodule() -> None:
    """
    Looking up an ordinary in-bundle `camera.<name>` must not import `camera.rigs` as a side effect.

    Importing each submodule to check for the name would register `rigs` tools on the way.
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

    IDEs and debuggers call `dir()` routinely.
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

    A missing name is checked against every submodule, `rigs` included, so ordering cannot help.
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

    `targeting` and `rigs` both export them, so this pins `rigs` last in `_SUBMODULES`.
    """
    tools = _tool_names_for_toolsets(
        "camera",
        prelude=("from blender_mcp.server.tools import camera\ncamera.FollowForwardAxis\ncamera.UpAxis\n"),
    )
    assert "create_orbit_camera_rig" not in tools, "resolving a name shared with rigs still leaked rigs"


def test_ast_derived_submodule_names_match_the_real_runtime_attributes() -> None:
    """
    `_submodule_top_level_names`'s static parse must agree with each submodule's real `vars()`.

    Catches a submodule using a binding form the parser misses. Runs in a subprocess because it
    imports every submodule.
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


@pytest.mark.parametrize("raw_value", [None, "shot", "camera-rigs", ALL_SENTINEL])
def test_mount_map_predicts_exactly_what_a_process_registers(raw_value: str | None) -> None:
    """
    `mount_map`'s parse must equal the tools a real process mounts, for every kind of selection.

    `get_addon_status` tells an agent which bundle would mount a tool it cannot call, and it
    reads that from source rather than by importing (importing registers tools, which is the
    thing a selection exists to prevent). A parse that drifted would send the agent to the
    wrong bundle, which is worse than the silence it replaced. The `None` and `all` cases pin
    the ends; `camera-rigs` is the selection the ambiguity was actually reported against.
    """
    expected = set(bundle_tool_names()[CORE_BUNDLE])
    for bundle in resolve_toolset_bundles(raw_value):
        expected |= bundle_tool_names()[bundle]
    assert set(_tool_names_for_toolsets(raw_value)) == expected


def test_mount_map_names_the_bundle_that_would_mount_an_unmounted_tool() -> None:
    """
    A tool absent from a selection must still be traceable to the bundle that provides it.

    This is the rehearsal failure the map exists for: `create_dolly_camera_rig` is registered,
    dispatch-wired and tested, but `shot` does not mount it, and the agent concluded it did not
    exist. Both halves matter - the name resolves to a bundle, and that bundle is not in `shot`.
    """
    assert bundles_providing("create_dolly_camera_rig") == ("camera-rigs",)
    assert "create_dolly_camera_rig" not in _tool_names_for_toolsets("shot")
    assert unmounted_bundle_counts(_tool_names_for_toolsets("shot"))["camera-rigs"] == len(
        bundle_tool_names()["camera-rigs"]
    )


def test_mount_map_knows_nothing_it_cannot_mount() -> None:
    """
    An unknown name must resolve to no bundle, so `in_this_build` can be trusted as a negative.

    Without this the verdict "implemented but not mounted" could be returned for a typo.
    """
    assert bundles_providing("create_teapot") == ()
    assert known_tool_names() == set(_tool_names_for_toolsets(ALL_SENTINEL))


def test_texture_lighting_alias_resolves_to_the_full_pre_split_surface() -> None:
    """The retired fused bundle name stays usable, losing no capability for an existing config."""
    expected = set(BUNDLES["texture"]) | set(BUNDLES["lighting"]) | set(BUNDLES["lighting-construction"])
    assert set(resolve_toolset_modules("texture-lighting")) - set(resolve_toolset_modules(None)) == expected


def test_shot_and_asset_modes_share_only_the_core_surface() -> None:
    """Selecting shot must not drag in asset authoring, and vice versa."""
    core = set(_tool_names_for_toolsets(None))
    shot = set(_tool_names_for_toolsets("shot")) - core
    asset = set(_tool_names_for_toolsets("asset")) - core

    assert shot and asset, "each mode must add tools of its own"
    assert shot.isdisjoint(asset), f"modes overlap outside core: {sorted(shot & asset)}"


def test_shot_mode_lights_the_shot_but_excludes_texture_authoring_and_rig_construction() -> None:
    """
    Shot places and aims lights; texture authoring and camera-rig construction stay opt-in.

    An interior shot has no usable light without placing one, so light construction is shot work.
    """
    shot = _tool_names_for_toolsets("shot")
    assert "configure_hdri_environment" in shot, "environment lighting must be in shot"
    assert shot >= {"create_light", "configure_light", "aim_light", "configure_light_linking", "create_studio_lighting"}
    assert not {t for t in shot if t.startswith(("create_pbr_material", "apply_pbr_texture_set"))}, (
        "texture authoring must not ride along with shot"
    )
    assert "create_orbit_camera_rig" not in shot, "rig construction belongs to camera-rigs, not shot"


POSING_TOOLS = frozenset(
    {
        "set_character_pose",
        "keyframe_character_pose",
        "list_character_bones",
        "probe_bone_axis",
        "solve_bone_reach",
        "keyframe_bone_reach",
        "sample_deformed_geometry",
    }
)


def test_shot_mode_can_pose_a_linked_character_without_rig_construction() -> None:
    """Posing a linked character is shot work; building or binding a rig is not."""
    shot = _tool_names_for_toolsets("shot")
    assert shot >= POSING_TOOLS
    assert not shot & {"create_armature", "bind_mesh_to_armature", "create_ik_fk_limb"}


def test_character_posing_bundle_adds_only_the_posing_tools() -> None:
    """The posing split must not drag rig construction in through a shared import."""
    assert _tool_names_for_toolsets("character-posing") - _tool_names_for_toolsets(None) == POSING_TOOLS


def test_character_rigging_bundle_keeps_every_rigging_tool_after_the_split() -> None:
    """Existing `character-rigging` configs lose nothing: all 27 tools, posing included."""
    rigging = _tool_names_for_toolsets("character-rigging") - _tool_names_for_toolsets(None)
    assert len(rigging) == 27
    assert rigging >= POSING_TOOLS | {"create_armature", "bind_mesh_to_armature", "add_pose_bone_constraint"}


def test_character_rigging_lazy_attribute_submodules_match_the_rigging_bundles() -> None:
    """Character-rigging equivalent of `test_camera_lazy_attribute_submodules_match_the_camera_bundles`."""
    assert set(character_rigging._SUBMODULES) == _dotted_submodule_names("character-rigging", "character-posing")


@pytest.mark.parametrize("raw_value", ["all", "ALL"])
def test_all_sentinel_selects_every_module(raw_value: str) -> None:
    """
    The `all` sentinel resolves to core plus every bundle, core first, with no duplicates.

    Recomputed from BUNDLES, since comparing with ALL_MODULES would miss a bug in building it.
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

    Uses `find_spec`, not import. Importing a tool module registers its tools on the shared
    FastMCP app, so later tests would see the full catalog. Nothing in this file may import a tool
    module in-process.
    """
    for module_name in ALL_MODULES:
        assert importlib.util.find_spec(f"blender_mcp.server.tools.{module_name}") is not None, (
            f"{module_name} is named by a bundle but does not exist"
        )


def _run_server_script(raw_value: str | None, script: str) -> str:
    """
    Run `script` in a fresh subprocess with BLENDER_MCP_TOOLSETS set to `raw_value`.

    The variable is removed from the inherited environment first, so a developer's own export
    cannot change what the tests measure.

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
    Collect the tool names a server process registers for a BLENDER_MCP_TOOLSETS value.

    Cached because each selection costs a full server import.

    Args:
        raw_value: The BLENDER_MCP_TOOLSETS value to set, or None to leave it unset.
        prelude: Statements run after `mcp` is imported and before the tool list is read,
            such as a `dir()` call whose side effects a test observes.

    Returns:
        The tool names advertised after `prelude` has run.

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
    Measure the advertised payload bytes for a BLENDER_MCP_TOOLSETS value.

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


# A ceiling, not a target: lower it when the payload shrinks. Raising it is a decision to record
# in the commit message.
#
# Raised from 250,000 by the fifth runbook-rehearsal pass, measured at 259,274 - 9,410 bytes.
# 4,951 is `probe_bone_axis`, the new measurement that replaces a caller's own headless
# axis-probing script: which local axis swings a limb, and which sign of a roll turns a palm, is
# not derivable from `list_character_bones(rest_axes=True)`'s nine rest numbers, and a cached
# out-of-band answer goes stale silently. 699 is `create_camera`'s `target_bone_name`, without
# which aiming at a character rig aims at its origin on the floor. The rest is the core surface
# below, which this one inherits. Earlier increases are recorded in their own commit messages.
#
# Raised from 259,500 for `sample_deformed_geometry`, measured at 262,266 - 2,766 bytes, the
# whole increase. It is the shot surface's only readback of evaluated geometry: every other
# reader here describes the base mesh at rest, so a posed character could be measured only by
# screenshot, and a weight transfer or a shape-key fix could not be verified at all.
#
# Raised from 262,500, measured at 263,199 - 699 bytes. 362 is `list_character_bones`'
# `deformed_meshes`: which meshes a rig moves was obtainable in a posing session only as a side
# effect of `frame_camera_on_objects`, which moves a camera to answer it, and it is the
# prerequisite for naming a mesh to `sample_deformed_geometry`. The rest is the core surface
# below, whose `set_action_cycle` contract changed with the INSPECT period fix.
#
# Raised from 263,500, measured at 264,930 - 1,430 bytes, all of it `set_object_visibility`.
# Deleting a library-override object through `remove_scene_objects` does not survive Blender's
# own liboverride resync: the next file load recreates it, silently undoing the deletion.
# `set_object_visibility` changes hide_render/hide_viewport/hide_select in place instead of
# removing the ID, so it survives that resync - the durable alternative `remove_scene_objects`'s
# new confirm_override_removal refusal now points callers at.
#
# Raised from 264,930, measured at 265,968 - 1,038 bytes. 702 is the core surface below. The
# rest is `render_scene`'s create_directories, which `save_shot` already had: a render into a
# shot's not-yet-made renders folder could only be refused, and `configure_world_background`'s
# one-line pointer to the canon-World path, which a rehearsal looked for there and did not find.
SHOT_MODE_BYTE_CEILING = 265_968

# The same rule as above, for the default, core-only surface.
#
# Raised from 85,500 by the fifth runbook-rehearsal pass, measured at 89,044 - 3,829 bytes.
# 1,788 is `remove_scene_objects`, moved out of the `scene-authoring` bundle: every other mode
# could create scratch objects and never delete one, and `manage_scene_collections` refuses to
# leave an object in zero collections, so a rehearsal's diagnostic objects were saved into the
# shot as permanent orphans. 1,732 is `get_addon_status`'s paged `mounted_tools` enumeration,
# which is how a session tells a tool it did not mount from one that does not exist - the
# single-name `tool_name` lookup could only answer that one guess at a time. 546 is
# `set_action_cycle`'s INSPECT operation, the non-destructive way to read a cycle's period that
# a rehearsal previously had to obtain by deleting the cycle. 394 is `validate_scene`'s `offset`
# and the paging contract its description now states, replacing a reply that told callers to
# continue from an offset the schema rejected. Earlier increases are recorded in their own
# commit messages, not here.
#
# Raised from 89,250, measured at 89,381 - 337 bytes, all of it `set_action_cycle`'s corrected
# INSPECT contract: a curve with no Cycles modifier reports `key_extent_frames` with a null
# `period_frames`, and the warnings compare only the cycled curves. Both halves have to be
# stated, because the old shape shipped this repo's first wrong warning - a deliberately
# uncycled track told it "drifts apart" from the stride - and a caller who believed the
# warnings needs to know which comparison is now being made.
#
# Raised from 89,500, measured at 90,907 - 1,407 bytes, all of it `set_object_visibility` (see
# the shot-ceiling comment above for why it exists).
#
# Raised from 90,907, measured at 91,609 - 702 bytes. `link_canon_library`'s `world`: a World
# is in no collection, so a canon library's World was unreachable by any linking call.
# `list_scene_objects`' `search`: on a 598-object set at 25 a page, finding one object meant
# paging the whole scene.
DEFAULT_MODE_BYTE_CEILING = 91_609


def test_shot_mode_payload_stays_under_its_ceiling() -> None:
    """
    The shot-assembly surface must not grow back. Lower is always acceptable.

    Measured by mode name, so it follows any edit to `MODES["shot"]`.
    """
    assert _payload_bytes_for_toolsets("shot") <= SHOT_MODE_BYTE_CEILING


def test_default_mode_payload_stays_under_its_ceiling() -> None:
    """The always-on core surface must not grow back either. Lower is always acceptable."""
    assert _payload_bytes_for_toolsets(None) <= DEFAULT_MODE_BYTE_CEILING


def test_selecting_a_bundle_adds_exactly_that_bundle_on_top_of_core() -> None:
    """
    Selecting one bundle adds its own tools to core and nothing else.

    Compares name sets, not counts, which would pass with the wrong tools added.
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

    Parsed inside the test, so a reworded docstring fails one test instead of collection.
    """
    quoted = re.search(r"\((\d+) tools, per", bundles.__doc__ or "")
    assert quoted, "bundles.py's docstring no longer quotes a tool count in the expected form"
    assert len(_tool_names_for_toolsets(ALL_SENTINEL)) == int(quoted.group(1))


def test_scene_authoring_tools_are_not_in_the_core_surface() -> None:
    """Geometry creation and the whole-scene reset must not ship in every process."""
    core_tools = _tool_names_for_toolsets(None)
    for name in _SCENE_AUTHORING_TOOLS:
        assert name not in core_tools, f"{name} is still registered by the core surface"


def test_scene_authoring_bundle_restores_them() -> None:
    """No capability is lost -- the same tools are reachable by asking for the bundle."""
    authoring_tools = _tool_names_for_toolsets("scene-authoring")
    for name in _SCENE_AUTHORING_TOOLS:
        assert name in authoring_tools, f"{name} is not reachable through the scene-authoring bundle"


def test_removing_a_named_object_is_core_not_an_authoring_bundle() -> None:
    """
    Every process can delete an object it created; no bundle has to be mounted for it.

    `manage_scene_collections` refuses to unlink an object from its last collection and refuses
    to remove a non-empty collection, so before this a session that made a scratch or diagnostic
    object had no way to take it back out: it was saved into the shot for good.
    """
    assert "remove_scene_objects" in _tool_names_for_toolsets(None)
    assert "remove_scene_objects" in _tool_names_for_toolsets("shot")
    assert bundles_providing("remove_scene_objects") == (CORE_BUNDLE,)


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
    Find the names not rendered as `` `name` `` anywhere in README.

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

    Only mode names: the bundles a mode names are covered by the bundle check.
    """
    missing_modes = _names_missing_from_readme(MODES)
    assert not missing_modes, f"modes missing from README's table: {missing_modes}"


def _tool_annotations_for_toolsets(raw_value: str | None) -> dict[str, dict[str, bool]]:
    """
    Read the destructive, read-only and open-world hints a server process advertises.

    In a subprocess because only an import through `tools/__init__` adds the annotations.

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
    The tools kept out of core because they clear a scene must say so on the wire.

    `reset_scene` matches no destructive prefix, so only its `_DESTRUCTIVE_TOOLS` entry marks it.
    """
    annotations = _tool_annotations_for_toolsets("scene-authoring")

    assert annotations["reset_scene"]["destructive"], "reset_scene is destructive but does not advertise it"
    assert not annotations["reset_scene"]["read_only"], "reset_scene mutates but advertises read_only"
    assert not annotations["create_geometry_object"]["destructive"], (
        "create_geometry_object adds an object; marking it destructive would devalue the hint"
    )


def test_remove_scene_objects_advertises_its_destructiveness_from_the_core_surface() -> None:
    """A core process still warns before deleting: the `remove_` prefix earns the hint by name."""
    annotations = _tool_annotations_for_toolsets(None)

    assert annotations["remove_scene_objects"]["destructive"], (
        "remove_scene_objects is destructive but does not advertise it"
    )
    assert not annotations["remove_scene_objects"]["read_only"], "remove_scene_objects mutates but advertises read_only"


def test_file_lifecycle_tools_are_exactly_eleven_and_reachable_from_shot_and_asset() -> None:
    """The eleven file-lifecycle/linking tools exist once each and are reachable from both modes."""
    assert len(_FILE_LIFECYCLE_TOOLS) == 11
    assert set(_FILE_LIFECYCLE_TOOLS) == set(_FILE_LIFECYCLE_HINTS)
    shot = _tool_names_for_toolsets("shot")
    asset = _tool_names_for_toolsets("asset")
    for name in _FILE_LIFECYCLE_TOOLS:
        assert name in shot, f"{name} is not reachable from the shot mode"
        assert name in asset, f"{name} is not reachable from the asset mode"


def test_file_lifecycle_tools_advertise_correct_hints() -> None:
    """
    All eleven tools advertise the right destructive/read-only/open-world hints, by name, both ways.

    Every tool gets all three hints, so a tool left out of a set ships a wrong hint, not none.
    """
    annotations = _tool_annotations_for_toolsets("shot")
    for name, (destructive, read_only, open_world) in _FILE_LIFECYCLE_HINTS.items():
        actual = annotations[name]
        assert actual["destructive"] == destructive, f"{name}: expected destructive={destructive}"
        assert actual["read_only"] == read_only, f"{name}: expected read_only={read_only}"
        assert actual["open_world"] == open_world, f"{name}: expected open_world={open_world}"


# Emitted for `_BLEND_FILE_TOOLS` in `_documentation.py`.
_BLEND_FILE_EFFECTS_SENTENCE = "reads or writes a .blend file on disk"
# Emitted for a tool in both `_BLEND_FILE_TOOLS` and the read-only prefix set.
_READ_ONLY_BLEND_FILE_SENTENCE = "Read-only for Blender data, but reads .blend files from disk."

_BLEND_FILE_TOOLS_UNDER_TEST = ("open_shot", "save_shot", "link_canon_library", "reload_library", "relocate_library")


def _tool_descriptions_for_toolsets(raw_value: str | None) -> dict[str, str]:
    """
    Read the full tool descriptions a server process advertises.

    In a subprocess because only an import through `tools/__init__` appends the contract text.

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
    None of the eleven claims it skips saving the .blend file; those that touch one say so correctly.

    Merging `_BLEND_FILE_TOOLS` into `_FILE_TOOLS`, or dropping its branch in `_tool_contract`,
    passes every other file-lifecycle test. `inspect_delivery` is read-only for Blender data and
    still reads `.blend` files from disk, so it gets its own accurate sentence rather than either
    of the other two.
    """
    descriptions = _tool_descriptions_for_toolsets("shot")
    for name in _FILE_LIFECYCLE_TOOLS:
        assert "does not save the .blend file" not in descriptions[name], (
            f"{name} carries the _FILE_TOOLS prose, which is false or misleading for it"
        )
    for name in _BLEND_FILE_TOOLS_UNDER_TEST:
        assert _BLEND_FILE_EFFECTS_SENTENCE in descriptions[name], f"{name} is missing the .blend-file effects sentence"
    assert _READ_ONLY_BLEND_FILE_SENTENCE in descriptions["inspect_delivery"]


def _parameter_descriptions_for_toolsets(raw_value: str | None) -> dict[str, str]:
    """
    Read every parameter description a server process advertises, nested models included.

    In a subprocess for the same reason as `_tool_descriptions_for_toolsets`.

    Args:
        raw_value: The BLENDER_MCP_TOOLSETS value to set, or None to leave it unset.

    Returns:
        "<tool>.<parameter>" mapped to its advertised description.

    """
    script = (
        "import asyncio, json\n"
        "from blender_mcp.server import mcp\n"
        "def walk(schema, prefix, out):\n"
        "    for name, property_schema in (schema.get('properties') or {}).items():\n"
        "        if isinstance(property_schema, dict) and 'description' in property_schema:\n"
        "            out[prefix + name] = property_schema['description']\n"
        "    for model, definition in (schema.get('$defs') or {}).items():\n"
        "        if isinstance(definition, dict):\n"
        "            walk(definition, prefix + model + '.', out)\n"
        "out = {}\n"
        "for tool in asyncio.run(mcp.list_tools()):\n"
        "    walk(tool.inputSchema, tool.name + '.', out)\n"
        "print(json.dumps(out))\n"
    )
    return json.loads(_run_server_script(raw_value, script))


# Prose forms of constraints the advertised JSON Schema already carries as keywords.
_CONSTRAINT_PROSE = ("Default:", "Allowed:", "Range:", "Must be at ", "Must be greater", "Must be less", "Requires ")

# Two adjacent single-letter words: what splicing a model title such as `EeveeLightingQuality`
# into each of its parameters produced ("e e v e e", "g i").
_SPLIT_ACRONYM_RE = re.compile(r"\b[a-z] [a-z]\b")


@pytest.mark.parametrize("raw_value", [None, "shot"])
def test_advertised_parameter_descriptions_do_not_restate_the_schema(raw_value: str | None) -> None:
    """
    Every description byte ships in each session's catalog, so repeating a keyword is pure cost.

    An authored docstring may still discuss a default in prose; only the generated
    "Default: ..."-style fragments are banned.
    """
    offenders = {
        parameter: description
        for parameter, description in _parameter_descriptions_for_toolsets(raw_value).items()
        if any(token in description for token in _CONSTRAINT_PROSE)
    }
    assert not offenders, f"parameter descriptions restating schema keywords: {offenders}"


@pytest.mark.parametrize("raw_value", [None, "shot"])
def test_advertised_parameter_descriptions_contain_no_letter_split_words(raw_value: str | None) -> None:
    """`EEVEE` must never reach an agent as "e e v e e"; nothing generated may splice a title in."""
    offenders = {
        parameter: description
        for parameter, description in _parameter_descriptions_for_toolsets(raw_value).items()
        if _SPLIT_ACRONYM_RE.search(description)
    }
    assert not offenders, f"parameter descriptions containing letter-split words: {offenders}"
