"""
Run with Blender 5.1+ to smoke-test a canon library's World and a face slider keyed on its override.

Both findings came from one rehearsal against a linked-and-overridden character, and both are
properties of real Blender a fake `bpy` cannot show:

- A World is in no collection, so linking the character's collection never brought the canon
  World; `link_canon_library(world=...)` links it and makes it the scene's world.
- A JSON `0` written into a float slider turned it into an int property, and Blender flags an
  F-Curve keyed on an int property to round everything it evaluates. The keys stayed exact while
  playback read 0.35 as 0 and 0.85 as 1. The keyed value has to survive a save and a reopen.
"""

import importlib.util
import sys
import tempfile

from pathlib import Path

import bpy

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon" / "__init__.py"
package_name = "blender_mcp_canon_override_smoke"
spec = importlib.util.spec_from_file_location(
    package_name,
    addon_path,
    submodule_search_locations=[str(addon_path.parent)],
)
assert spec is not None
addon = importlib.util.module_from_spec(spec)
sys.modules[package_name] = addon
spec.loader.exec_module(addon)

from blender_mcp_canon_override_smoke.handlers.character_rigging import (  # ruff: ignore[module-import-not-at-top-of-file]
    CharacterRiggingHandlersMixin,
)
from blender_mcp_canon_override_smoke.handlers.linking import (  # ruff: ignore[module-import-not-at-top-of-file]
    LinkingHandlersMixin,
)

SLIDER = "expr_smile"
CHARACTER_COLLECTION = "Canon Character"
RIG = "CanonRig"
CANON_WORLD = "Canon World"
# The rehearsal's own sequence: rest at 0 spelled as a JSON integer, the smile, and rest again.
# The last key is the one whose type decided how Blender flagged the whole curve.
KEYS = ((1.0, 0), (20.0, 0.35), (40.0, 0.85), (60.0, 0))
TOLERANCE = 1e-6


def _build_library(path: Path) -> None:
    """Write a canon file holding a rig with one float slider, and a World no collection holds."""
    bpy.ops.wm.read_homefile(use_empty=True)
    collection = bpy.data.collections.new(CHARACTER_COLLECTION)
    bpy.context.scene.collection.children.link(collection)
    data = bpy.data.armatures.new(f"{RIG}Data")
    rig = bpy.data.objects.new(RIG, data)
    collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    head = data.edit_bones.new("head")
    head.head, head.tail = (0.0, 0.0, 1.5), (0.0, 0.0, 1.7)
    bpy.ops.object.mode_set(mode="OBJECT")
    pose_bone = rig.pose.bones["head"]
    pose_bone[SLIDER] = 0.0
    pose_bone.id_properties_ui(SLIDER).update(min=0.0, max=1.0, soft_min=0.0, soft_max=1.0)
    world = bpy.data.worlds.new(CANON_WORLD)
    world.use_fake_user = True
    bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True)


def _slider_curve(rig):
    action = rig.animation_data.action
    wanted = f'pose.bones["head"]["{SLIDER}"]'
    for layer in action.layers:
        for strip in layer.strips:
            for channelbag in strip.channelbags:
                curve = channelbag.fcurves.find(wanted, index=0)
                if curve is not None:
                    return curve
    raise AssertionError(f"no curve keyed {wanted}")


def _played_back(rig) -> dict[float, float]:
    curve = _slider_curve(rig)
    return {frame: curve.evaluate(frame) for frame, _value in KEYS}


def _check_world(library_path: Path) -> None:
    """Linking the World, and the refusals that name what the call accepts."""
    try:
        LinkingHandlersMixin.link_canon_library(filepath=str(library_path))
    except ValueError as exc:
        assert "world=" in str(exc), str(exc)
    else:
        raise AssertionError("a link naming nothing was accepted")
    try:
        LinkingHandlersMixin.link_canon_library(filepath=str(library_path), world="No Such World")
    except ValueError as exc:
        assert "No Such World" in str(exc), str(exc)
    else:
        raise AssertionError("a World the library lacks was linked")
    assert not bpy.data.libraries, "a refused link left a Library behind"


def main() -> None:
    """Link a canon rig as an override with its World, key its slider, and read it back from disk."""
    with tempfile.TemporaryDirectory() as tmp:
        library_path = Path(tmp).resolve() / "canon_character.blend"
        shot_path = Path(tmp).resolve() / "sh030.blend"
        _build_library(library_path)

        bpy.ops.wm.read_homefile(use_empty=True)
        scene = bpy.context.scene
        scene.world = bpy.data.worlds.new("Shot World")
        _check_world(library_path)

        linked = LinkingHandlersMixin.link_canon_library(
            filepath=str(library_path), collections=[CHARACTER_COLLECTION], world=CANON_WORLD, as_override=True
        )
        assert scene.world is not None and scene.world.name == CANON_WORLD, scene.world
        assert scene.world.library is not None, "the scene's world is not the library's"
        assert linked["world"]["name"] == CANON_WORLD, linked
        assert linked["previous_world"] == "Shot World", linked
        assert any("Shot World" in warning for warning in linked["warnings"]), linked
        assert linked["changed_resources"] == [CANON_WORLD], linked

        rig = bpy.data.objects[RIG]
        assert rig.override_library is not None, "expected the rig as a library override"
        assert isinstance(rig.pose.bones["head"][SLIDER], float)

        handler = CharacterRiggingHandlersMixin()
        for frame, value in KEYS:
            handler.keyframe_character_pose(
                RIG, "SMOKE_slider", frame, [{"bone_name": "head", "custom_properties": {SLIDER: value}}]
            )
        assert isinstance(rig.pose.bones["head"][SLIDER], float), "an integral key retyped the slider"
        before_save = _played_back(rig)
        for frame, value in KEYS:
            assert abs(before_save[frame] - value) < TOLERANCE, f"frame {frame} plays {before_save[frame]}"

        bpy.ops.wm.save_as_mainfile(filepath=str(shot_path))
        bpy.ops.wm.open_mainfile(filepath=str(shot_path), load_ui=False)
        reopened = bpy.data.objects[RIG]
        after_reopen = _played_back(reopened)
        for frame, value in KEYS:
            assert abs(after_reopen[frame] - value) < TOLERANCE, (
                f"frame {frame} was keyed {value} and plays back {after_reopen[frame]} after reopening"
            )
        reopened_world = bpy.context.scene.world
        assert reopened_world is not None and reopened_world.name == CANON_WORLD, reopened_world
        assert reopened_world.library is not None, "the linked World did not survive the save"

    print(f"canon override: slider played back {after_reopen} after reopen; world {CANON_WORLD!r} linked")
    print("CANON_OVERRIDE_SMOKE_OK")


if __name__ == "__main__":
    main()
