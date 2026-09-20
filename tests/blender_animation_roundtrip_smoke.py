# ruff: file-ignore[module-import-not-at-top-of-file]
"""
Run with Blender 5.1+ to prove MCP-authored animation survives a save and reopen as editable data.

`tests/blender_animation_smoke.py` exercises the Action handlers in one session. This script
asks the only question that session cannot answer: after `save_shot` writes the file and
`open_shot` reads it back, is the animation still there, still sparse, and still an artist's to
edit? The defect this guards is the one that started the artefact-truth work - an action the
agent authored, left with no user, silently dropped at save - so the reopen is the whole point.

Keyframes, a driver and an NLA strip are round-tripped together, because each is kept alive by
a different mechanism: an assigned action has a real user, a driver lives on the object's
animation data, and a strip holds its own action reference.
"""

import importlib.util
import sys
import tempfile

from pathlib import Path

import bpy

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon" / "__init__.py"
package_name = "blender_mcp_animation_roundtrip_smoke"
spec = importlib.util.spec_from_file_location(
    package_name,
    addon_path,
    submodule_search_locations=[str(addon_path.parent)],
)
assert spec is not None
addon = importlib.util.module_from_spec(spec)
sys.modules[package_name] = addon
spec.loader.exec_module(addon)

from blender_mcp_animation_roundtrip_smoke.server_core import BlenderMCPServer

ACTION_NAME = "Roundtrip Motion"
TRACK_NAME = "Roundtrip Track"
STRIP_NAME = "Roundtrip Strip"
STRIP_ACTION_NAME = "Roundtrip Strip Motion"


def _run(server: BlenderMCPServer, command: str, **params: object) -> dict:
    """
    Dispatch one command the way the socket server does, and require success.

    Args:
        server: The server under test.
        command: The MCP command type.
        **params: The command's parameters.

    Returns:
        dict: The command's result.

    Raises:
        AssertionError: When the command failed.

    """
    response = server.execute_command_internal({"type": command, "params": params})
    assert response["status"] == "success", (command, response)
    return response["result"]


def main() -> None:
    """Write animation, save, reopen, and read every part of it back."""
    server = BlenderMCPServer()
    # Commands go through `execute_command_internal`, the entry point the socket server calls,
    # so the transaction wrapper and the real dispatch table are both in play. That table reads
    # the add-on's own scene properties, which only `register()` installs.
    addon.register()
    addon.session.register_handlers()
    scene = bpy.context.scene
    work = Path(tempfile.mkdtemp(prefix="animation_roundtrip_"))
    blend_path = work / "sh040.blend"

    hero = bpy.data.objects.new("Hero", bpy.data.meshes.new("HeroMesh"))
    scene.collection.objects.link(hero)
    target = bpy.data.objects.new("Driver Target", None)
    scene.collection.objects.link(target)

    # Two sparse keys, not a baked range: what an artist can retime is the difference.
    keyed = _run(
        server,
        "keyframe_object_transform",
        keyframes=[
            {"object_name": "Hero", "frame": 1, "location": [0.0, 0.0, 0.0]},
            {"object_name": "Hero", "frame": 24, "location": [5.0, 0.0, 2.0]},
        ],
    )
    assert keyed["keyframes"], keyed
    authored_action = hero.animation_data.action
    assert authored_action is not None
    authored_action.name = ACTION_NAME
    # An assigned action has a real user, which is what carries it through the save. Nothing
    # sets a fake user, so an action nobody assigned would be dropped - and reported.
    assert authored_action.users >= 1, authored_action.users

    _run(
        server,
        "manage_animation_driver",
        target={"type": "OBJECT", "name": "Hero"},
        action="ADD",
        data_path="scale",
        array_index=0,
        driver_type="SCRIPTED",
        expression="1.5",
    )

    _run(
        server,
        "manage_animation_action",
        target={"type": "OBJECT", "name": "Driver Target"},
        action="CREATE",
        action_name=STRIP_ACTION_NAME,
    )
    _run(
        server,
        "manage_nla_tracks",
        target={"type": "OBJECT", "name": "Driver Target"},
        action="CREATE_TRACK",
        track_name=TRACK_NAME,
    )
    _run(
        server,
        "manage_nla_tracks",
        target={"type": "OBJECT", "name": "Driver Target"},
        action="ADD_STRIP",
        track_name=TRACK_NAME,
        strip_name=STRIP_NAME,
        action_name=STRIP_ACTION_NAME,
        frame_start=1,
    )

    # Nothing the agent authored may be sitting unreferenced: that is the defect this whole
    # check exists for, and `persistence` is where it would show.
    findings = _run(server, "validate_scene", scene_name=scene.name, scope=["persistence"])["findings"]
    authored_names = {ACTION_NAME, STRIP_ACTION_NAME}
    discarded = {
        finding["subject"] for finding in findings if finding["code"] == "UNREFERENCED_DATABLOCK"
    } & authored_names
    assert not discarded, f"authored animation would be discarded at save: {sorted(discarded)}"

    saved = _run(server, "save_shot", filepath=str(blend_path))
    assert saved["provenance_written"] is True, saved
    assert blend_path.is_file()

    # The reopen is the check. Everything before it is intent.
    _run(server, "open_shot", filepath=str(blend_path))
    # macOS resolves the temp directory through /private, so compare canonical paths.
    assert Path(bpy.data.filepath).resolve() == blend_path.resolve()

    reopened_hero = bpy.data.objects.get("Hero")
    assert reopened_hero is not None
    assert reopened_hero.animation_data is not None
    assert reopened_hero.animation_data.action is not None
    assert reopened_hero.animation_data.action.name == ACTION_NAME

    inspected = _run(server, "inspect_animation", target={"type": "OBJECT", "name": "Hero"})
    assert inspected["action"]["name"] == ACTION_NAME, inspected["action"]
    assert inspected["action"]["frame_range"] == [1.0, 24.0], inspected["action"]
    frames = sorted({key["frame"] for key in inspected["keyframes"]})
    assert frames == [1.0, 24.0], frames
    # Sparse, editable keys: six channels (location xyz at two frames), not 24 baked samples.
    assert inspected["total_keyframes"] == 6, inspected["total_keyframes"]
    assert {key["interpolation"] for key in inspected["keyframes"]} == {"BEZIER"}, inspected["keyframes"]

    driver_paths = {driver["data_path"] for driver in inspected["drivers"]}
    assert "scale" in driver_paths, inspected["drivers"]

    strips = _run(server, "inspect_animation", target={"type": "OBJECT", "name": "Driver Target"})
    tracks = {track["name"]: track for track in strips["nla_tracks"]}
    assert TRACK_NAME in tracks, tracks
    assert any(strip["action"] == STRIP_ACTION_NAME for strip in tracks[TRACK_NAME]["strips"]), tracks

    # The file is the product: it names its own author, and it is portable enough to hand over.
    delivery = _run(server, "inspect_delivery", scene_name=bpy.context.scene.name)
    assert delivery["saved"] is True
    assert delivery["provenance"]["valid"] is True, delivery["provenance"]

    print("ANIMATION_ROUNDTRIP_SMOKE_OK")


if __name__ == "__main__":
    main()
