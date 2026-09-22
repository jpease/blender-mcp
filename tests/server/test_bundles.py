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

# Reachable only through `scene-authoring`.
_SCENE_AUTHORING_TOOLS = ("create_geometry_object", "reset_scene", "remove_scene_objects")

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
        "solve_bone_reach",
        "keyframe_bone_reach",
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
    """Existing `character-rigging` configs lose nothing: all 25 tools, posing included."""
    rigging = _tool_names_for_toolsets("character-rigging") - _tool_names_for_toolsets(None)
    assert len(rigging) == 25
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
# Raised once, from 193,438, for the animation-as-editable-data work. Measured split of the
# 8,103 bytes: 6,053 is the `BoneAim`/`BoneRotation` JSON Schema, emitted once each by
# `set_character_pose` and `keyframe_character_pose`, which is what lets an agent aim a bone at
# the camera instead of guessing bone-local radians; 2,050 is Eevee's ray-tracing patch, which
# is what stops the demo set's glass rendering black without a manual trip through Blender's UI.
# 895 bytes of Args rows that only restated the schema were deleted in the same pass.
# Raised a second time, from 202,000, by the artefact-truth work: `inspect_delivery` joined the
# core surface (the only tool that reads a file's external references back and answers whether
# they travel), `render_scene` gained confirm_frame_range/persist_output/detail plus an optional
# filepath so a render can carry its own intent, and `save_shot` gained the two provenance
# switches. 601 of the 3,260 bytes are `save_shot`'s.
# Raised a third time, from 205,260, by `list_character_bones(bone_names=…)`: 459 bytes that
# turn reading three bones' rest axes off a 187-bone rig from six paginated calls (53,023
# bytes) into one 997-byte reply.
# Raised a fourth time, from 205,719, by two changes measured apart: 4,490 bytes are
# `solve_bone_reach`'s `BoneReach` schema and tool description (payload_report with and without
# that one tool), which is what turns "bend this chain until the hand lands on that point" from
# roughly fourteen guessed `rotate` calls into one; the other 1,986 are
# `get_viewport_screenshot`'s new `view` (ViewSpec) and `shading_override` parameters, which
# also land in the core surface below. Measured shot payload 212,711 bytes.
# Raised a fifth time, from 212,711, by `solve_bone_reach`'s convergence contract: 810 bytes
# (payload_report with the tool module at HEAD and with the change) for the `tolerance_m`
# parameter and the Returns rows naming converged/chain_reach_m/target_distance_m/
# out_of_reach. That is what turns a bare `achieved_error_m` float into an answer an agent can
# act on - retry, move the rig, or accept - instead of a number it has no threshold for.
# Measured shot payload 213,521 bytes.
# Raised a sixth time, from 213,521, by the silent-failure pass, measured per file by swapping
# that one module back to HEAD: 1,418 bytes are `keyframe_object_transform`'s action surface
# (action_name/action_policy/action_slot_identifier/confirm_displace_action plus the paragraph
# naming the trap), 670 are the matching `keyframe_character_pose` policy and displacement
# wording, 750 are `point_camera_at`'s `camera_location`, and 286 are `get_addon_status`'s two
# new staleness fields. The first two are what stop a rig's root motion from being silently
# discarded when a pose is keyed into a new action - the failure mode cost a shot's worth of
# renders before anyone noticed the characters had stopped moving. Measured shot payload
# 216,645 bytes.
# Raised a seventh time, from 216,645, by the character-animation pass, measured per tool with
# `payload_report(...).per_tool`: 8,330 bytes are `keyframe_bone_reach`, 3,596 `set_action_cycle`
# and 1,659 `set_scene_frame`; the remaining 2,981 are the shared key-style vocabulary reaching
# the tools that already keyed - Blender's real 13-member `Keyframe.interpolation` enum instead
# of the 3 members this surface had been carrying, plus handle_left/handle_right/easing on
# `keyframe_character_pose`, `edit_keyframes` and `bake_evaluated_animation`. That is what a
# walk cycle costs: one call plants a foot across a frame range with real IK instead of two
# calls and a 4x4 matrix round-trip per foot per frame, the playhead can be moved so the agent
# can look at frame 12 instead of only frame 1, and pose keys can be shaped like every other
# domain's. Measured shot payload 233,211 bytes.
# Raised an eighth time, from 233,211, by the runbook-rehearsal pass - the defects a live
# rehearsal of the walk-cycle surface actually hit. Measured per tool with
# `payload_report(...).per_tool`, against each file's previous revision:
#   1,056 the axis-frame work on the posing tools. A runbook read `CHAR1_head_jnt`'s reported
#     rest axes, concluded `up_axis: "-X"`, and shipped a head tilted 90 degrees; the same
#     file was then cached and reused. Measured on a synthetic bone carrying those exact axes,
#     "-X" is correct and the conventions already agreed, so what is bought here is the
#     derivation not being one: 479 is `list_character_bones` reporting the up axis it resolves
#     to and saying which frame the nine numbers are in, 366 is `BoneAim`'s own statement that
#     its letters are bone axes (183 apiece, once per pose tool through the shared schema), and
#     211 is `set_character_pose` correcting "a signed bone axis name" for `rotate`, whose
#     letters are measurably the call's `space`, not the bone's.
#   452 `render_scene`'s paragraph on a long ANIMATION outliving the client's request timeout:
#     the frames keep landing, so the move is `inspect_render_output`, not a re-render.
#   252 `set_action_cycle`'s reply contract - period_frames/first_key_frame/last_key_frame and
#     the notice that an unscoped cycle's shortened `modifiers` page cannot be resumed. Both
#     halves are that one tool's description and do not separate at tool granularity.
# The camera-marker retroactive-binding fix in the same pass cost nothing here: it is entirely
# handler-side (`handlers/camera/shots.py`), with no server-tool signature or docstring change.
# Measured shot payload 235,043 bytes.
# Raised a ninth time, from 235,043, by the second runbook-rehearsal pass - eight defects a live
# socket rehearsal hit, of which five cost catalog bytes. Measured per tool against HEAD with
# `payload_report(...).per_tool`:
#   2,690 `keyframe_character_pose`: the batched `keys=[{frame, poses}, ...]` form and its
#     preconditions, plus `BoneAim`'s `target_bone`/`target_bone_position` through the shared
#     schema. A thirteen-key stride was thirteen round trips, and "look at each other" aimed at
#     an armature origin on the floor, so both characters stared at each other's feet.
#   2,134 `set_action_cycle`: `mode_before` defaulting to NONE with its rationale,
#     `expected_period_frames`, the restricted range, and the paragraph stating that the period
#     is the curve's own key extent - a rehearsal measured `period_frames: 60.0` on a curve it
#     believed was looping at 20, and the reply had reported success.
#   1,518 `frame_camera_on_objects`: `bone_targets`, which is how a close-up is framed on a rig
#     bone instead of on a guess about which meshes make up the region.
#   1,311 `set_character_pose`: the same `BoneAim` growth, and the corrected axis instruction -
#     the previous text told callers to pass `length_axis` as `track_axis`, which the tool then
#     refused, because Blender builds every bone along its own Y.
#   406 `list_character_bones`: `aim_axis_for_world`, the measured bone axis for each of the six
#     world directions, which is the answer the refusal above left unreported.
#   1,191 the file-lifecycle surface stating the path-redaction rule once per tool
#     (`get_session_info` and `list_libraries` 302 each, `inspect_delivery` 259, `open_shot` 225,
#     `unlink_libraries` 54, `reload_library` and `relocate_library` 49 each): a leaf-reduced
#     path was indistinguishable from a broken one, and `is_relative: false` on it read as a
#     defect.
#   126 `point_camera_at`: one sentence stating that `subtarget` aims at the bone's evaluated
#     world head, verified against real Blender rather than changed.
# The `validate_scene` engine probe and the UV-layer staleness fix in the same pass cost nothing
# here: both are handler-side only. Measured shot payload 244,468 bytes.
SHOT_MODE_BYTE_CEILING = 245_500

# The same rule for the default, core-only surface, and the same work: 166 bytes for
# `validate_scene`'s `persistence` scope, 2,202 for `inspect_delivery`, 601 for `save_shot`'s
# provenance switches. Raised from 69,831 by `get_viewport_screenshot`'s `view`/
# `shading_override` parameters, the only core-surface growth in that pass: 1,986 bytes, for a
# measured 71,817. No posing tool is in core, so none of `solve_bone_reach` is in this figure.
# Raised again from 71,817 by the two core-surface halves of the silent-failure pass: 1,418 for
# `keyframe_object_transform`'s action surface (`object_animation` is core, so root motion is
# keyed from every mode) and 286 for `get_addon_status`'s missing_commands/missing_parameters,
# which is how an agent now learns its add-on predates the server instead of concluding a tool
# does not exist. Measured core payload 73,521 bytes.
# Raised again from 73,521 by the core-surface half of the character-animation pass: 3,596 for
# `set_action_cycle` (an action that does not loop is not a cycle) and 1,659 for
# `set_scene_frame`, plus 1,058 for the widened key-style vocabulary on `edit_keyframes`,
# `bake_evaluated_animation` and `keyframe_object_transform`. `keyframe_bone_reach` is a posing
# tool, so none of its 8,330 bytes are in this figure. Measured core payload 79,834 bytes.
# Unchanged by the runbook-rehearsal pass beyond `set_action_cycle`'s 252 bytes, which is the
# only core-surface tool it touched: no posing tool is in core, and `render_scene` is not in
# the default surface either. Measured core payload 80,158 bytes, still inside this ceiling.
# Raised from 80,500 by the core-surface half of the second runbook-rehearsal pass: 2,134 for
# `set_action_cycle` (`object_animation` and `animation` are core, so a cycle is set from every
# mode) and 1,240 for the file-lifecycle surface stating the path-redaction rule once per tool.
# The posing and camera growth of that pass is absent here, as neither bundle is core, and the
# `validate_scene` engine probe is handler-side. Measured core payload 83,532 bytes.
DEFAULT_MODE_BYTE_CEILING = 84_000


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
    The tools moved out of core because they are destructive must say so on the wire.

    `reset_scene` matches no destructive prefix, so only its `_DESTRUCTIVE_TOOLS` entry marks it.
    """
    annotations = _tool_annotations_for_toolsets("scene-authoring")

    for name in ("reset_scene", "remove_scene_objects"):
        assert annotations[name]["destructive"], f"{name} is destructive but does not advertise it"
        assert not annotations[name]["read_only"], f"{name} mutates but advertises read_only"
    assert not annotations["create_geometry_object"]["destructive"], (
        "create_geometry_object adds an object; marking it destructive would devalue the hint"
    )


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
