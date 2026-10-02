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

import sys
import tempfile

from pathlib import Path

import bpy

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

addon = load_addon("blender_mcp_animation_roundtrip_smoke")

from blender_mcp_animation_roundtrip_smoke.server_core import BlenderMCPServer

ACTION_NAME = "Roundtrip Motion"
TRACK_NAME = "Roundtrip Track"
STRIP_NAME = "Roundtrip Strip"
STRIP_ACTION_NAME = "Roundtrip Strip Motion"
CHAR1_ACTION = "CHAR1_sh050_motion"
CHAR2_ACTION = "CHAR2_sh050_motion"


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


def _build_rig(name: str, location: tuple[float, float, float]) -> bpy.types.Object:
    """
    Build a two-bone rig at a location, the smallest thing `keyframe_character_pose` accepts.

    Args:
        name: Object name; its armature data takes the same name plus `Data`.
        location: Where to stand it, so the two rigs are not coincident.

    Returns:
        bpy.types.Object: The armature object, left in Object Mode.

    """
    data = bpy.data.armatures.new(f"{name}Data")
    rig = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(rig)
    rig.location = location
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    spine = data.edit_bones.new("spine")
    spine.head, spine.tail = (0, 0, 0), (0, 0, 0.4)
    head = data.edit_bones.new("head")
    head.head, head.tail = (0, 0, 0.4), (0, 0, 0.6)
    head.parent, head.use_connect = spine, True
    bpy.ops.object.mode_set(mode="OBJECT")
    rig.select_set(False)
    return rig


def _check_two_characters_keep_their_own_animation(server: BlenderMCPServer) -> None:
    """
    Two rigs, two actions, one shot: the case a two-character scene actually is.

    Blender 4.4+ actions are slotted, so one action datablock can carry several animated
    IDs. That makes cross-assignment the failure to guard: keying the second character must
    not land in the first character's action, retarget its slot, or overwrite its keys - and
    a save must carry both independently. Nothing in a single session would notice, because
    each rig evaluates correctly right up until the file is reopened.

    Args:
        server: The server under test, with a shot already saved and reopened.

    Raises:
        AssertionError: When either character's animation did not survive intact.

    """
    work = Path(tempfile.mkdtemp(prefix="animation_roundtrip_pair_"))
    blend_path = work / "sh050.blend"
    char1 = _build_rig("CHAR1_rig", (0.0, 0.0, 0.0))
    char2 = _build_rig("CHAR2_rig", (1.0, 0.0, 0.0))

    for rig, action_name, degrees in ((char1, CHAR1_ACTION, 25.0), (char2, CHAR2_ACTION, -25.0)):
        for frame, policy, turn in ((1, "CREATE", 0.0), (24, "REUSE", degrees)):
            _run(
                server,
                "keyframe_character_pose",
                armature_object_name=rig.name,
                action_name=action_name,
                frame=frame,
                poses=[{"bone_name": "head", "rotate": {"axis": "Z", "degrees": turn}}],
                space="LOCAL",
                action_policy=policy,
            )

    # Two actions, not one shared between them, and neither rig borrowed the other's.
    assert char1.animation_data.action.name == CHAR1_ACTION
    assert char2.animation_data.action.name == CHAR2_ACTION
    assert char1.animation_data.action is not char2.animation_data.action

    _run(server, "save_shot", filepath=str(blend_path))
    _run(server, "open_shot", filepath=str(blend_path))

    for rig_name, action_name in (("CHAR1_rig", CHAR1_ACTION), ("CHAR2_rig", CHAR2_ACTION)):
        reopened = bpy.data.objects.get(rig_name)
        assert reopened is not None, rig_name
        assert reopened.animation_data is not None, rig_name
        assert reopened.animation_data.action is not None, rig_name
        assert reopened.animation_data.action.name == action_name, (rig_name, reopened.animation_data.action.name)
        inspected = _run(server, "inspect_animation", target={"type": "OBJECT", "name": rig_name})
        assert inspected["action"]["name"] == action_name, inspected["action"]
        assert inspected["action"]["frame_range"] == [1.0, 24.0], (rig_name, inspected["action"])
        assert inspected["total_keyframes"] > 0, rig_name
        # Every channel belongs to the bone this character was posed on, not the other's rig.
        paths = {key["data_path"] for key in inspected["keyframes"]}
        assert paths and all('pose.bones["head"]' in path for path in paths), (rig_name, paths)

    # The two characters turned opposite ways, so identical curves would mean one overwrote
    # the other - the failure a shared action datablock produces.
    char1_keys = _run(server, "inspect_animation", target={"type": "OBJECT", "name": "CHAR1_rig"})["keyframes"]
    char2_keys = _run(server, "inspect_animation", target={"type": "OBJECT", "name": "CHAR2_rig"})["keyframes"]
    char1_values = [key["value"] for key in char1_keys]
    char2_values = [key["value"] for key in char2_keys]
    assert char1_values != char2_values, (char1_values, char2_values)


def _action_channels(action: bpy.types.Action) -> set[tuple[str, int]]:
    """
    Every `(data_path, array_index)` an action keys, across its layers and slots.

    Args:
        action: The action to read.

    Returns:
        set: Its channels.

    """
    return {
        (curve.data_path, curve.array_index)
        for layer in action.layers
        for strip in layer.strips
        for bag in strip.channelbags
        for curve in bag.fcurves
    }


def _hidden_strip_notices(reply: dict) -> list[str]:
    """Pick the hidden-strip notices out of one reply, leaving the transaction's own notices behind."""
    return [warning for warning in reply.get("warnings", ()) if "over the NLA at REPLACE" in warning]


def _check_an_unassigned_clip_plays_from_its_strip(server: BlenderMCPServer) -> None:
    """
    Key clips without assigning them, put them in NLA strips, and see which ones the active action hides.

    A Blender 5.x action holds one layer, so layered motion is NLA strips - and keying a clip
    used to mean assigning it, which displaced the root motion every time. With
    assign_action=False the clip takes the keys while the rig keeps its action, slot, pose and
    transform. A clip keying the root's own channels is then overridden by the active action at
    REPLACE, influence 1, and ADD_STRIP says so; one keying other channels plays and says nothing.

    Args:
        server: The server under test.

    Raises:
        AssertionError: When the rig was moved off its action, a clip missed its keys, or the
            hidden-strip warning was wrong either way.

    """
    rig = _build_rig("CHAR3_rig", (0.0, 3.0, 0.0))
    target = {"type": "OBJECT", "name": rig.name}
    for frame, x in ((1, 0.0), (24, 4.0)):
        _run(
            server,
            "keyframe_object_transform",
            keyframes=[{"object_name": rig.name, "frame": frame, "space": "LOCAL", "location": [x, 3.0, 0.0]}],
            action_name="CHAR3_root",
        )
    bpy.context.scene.frame_set(1)
    root, root_slot = rig.animation_data.action, rig.animation_data.action_slot.identifier
    location = tuple(rig.location)
    head_basis = rig.pose.bones["head"].matrix_basis.copy()

    waved = _run(
        server,
        "keyframe_character_pose",
        armature_object_name=rig.name,
        action_name="CHAR3_wave",
        keys=[
            {"frame": 1.0, "poses": [{"bone_name": "head", "rotate": {"axis": "Z", "degrees": 0.0}}]},
            {"frame": 12.0, "poses": [{"bone_name": "head", "rotate": {"axis": "Z", "degrees": 40.0}}]},
        ],
        assign_action=False,
    )
    hopped = _run(
        server,
        "keyframe_object_transform",
        keyframes=[{"object_name": rig.name, "frame": 6, "space": "LOCAL", "location": [9.0, 9.0, 9.0]}],
        action_name="CHAR3_hop",
        assign_action=False,
    )
    edited = _run(
        server,
        "edit_keyframes",
        target=target,
        edits=[{"data_path": "scale", "array_index": 2, "frame": 3, "value": 2.0}],
        action_name="CHAR3_squash",
        assign_action=False,
    )

    # The rig is exactly where, and on exactly what, it was.
    assert rig.animation_data.action == root, rig.animation_data.action
    assert rig.animation_data.action_slot.identifier == root_slot
    assert tuple(rig.location) == location, (tuple(rig.location), location)
    assert all(
        abs(a - b) < 1e-9
        for row_a, row_b in zip(rig.pose.bones["head"].matrix_basis, head_basis, strict=True)
        for a, b in zip(row_a, row_b, strict=True)
    )
    for reply in (waved, hopped, edited):
        assert reply["assigned_action"] == "CHAR3_root", reply
        # Nothing uses a clip yet, and an unused action does not survive a save: the reply says so
        # as data, and keeps its warnings for what is wrong.
        assert reply["keyed_action_users"] == 0, reply
        assert not any("ADD_STRIP" in warning for warning in reply["warnings"]), reply
    assert {path for path, _index in _action_channels(bpy.data.actions["CHAR3_wave"])} == {
        'pose.bones["head"].rotation_quaternion'
    }
    assert _action_channels(bpy.data.actions["CHAR3_hop"]) == {("location", index) for index in range(3)}
    assert _action_channels(bpy.data.actions["CHAR3_squash"]) == {("scale", 2)}

    # A rig that arrived with no animation data leaves without any.
    prop = bpy.data.objects.new("CHAR3_prop", None)
    bpy.context.scene.collection.objects.link(prop)
    _run(
        server,
        "keyframe_object_transform",
        keyframes=[{"object_name": prop.name, "frame": 1, "location": [1.0, 0.0, 0.0]}],
        action_name="CHAR3_prop_clip",
        assign_action=False,
    )
    assert prop.animation_data is None
    assert _action_channels(bpy.data.actions["CHAR3_prop_clip"]) == {("location", index) for index in range(3)}

    _run(server, "manage_nla_tracks", target=target, action="CREATE_TRACK", track_name="Gestures")
    wave_strip = _run(
        server,
        "manage_nla_tracks",
        target=target,
        action="ADD_STRIP",
        track_name="Gestures",
        strip_name="wave",
        action_name="CHAR3_wave",
        frame_start=1,
    )
    # The transaction adds its own background-mode undo notice; only the NLA notice is in question.
    assert not _hidden_strip_notices(wave_strip), wave_strip
    hop_strip = _run(
        server,
        "manage_nla_tracks",
        target=target,
        action="ADD_STRIP",
        track_name="Gestures",
        strip_name="hop",
        action_name="CHAR3_hop",
        frame_start=30,
    )
    (hidden,) = _hidden_strip_notices(hop_strip)
    assert "'CHAR3_root'" in hidden and "'hop'" in hidden and "overriding 3 channel(s)" in hidden, hidden
    # Keying into the active action says the same, because those keys are what does the hiding.
    rekeyed = _run(
        server,
        "keyframe_object_transform",
        keyframes=[{"object_name": rig.name, "frame": 24, "space": "LOCAL", "location": [4.0, 3.0, 0.0]}],
        action_name="CHAR3_root",
    )
    assert ["'hop'" in notice for notice in _hidden_strip_notices(rekeyed)] == [True], rekeyed

    # And the wave really plays from its strip, under the root motion that kept driving the rig.
    bpy.context.scene.frame_set(12)
    turned = rig.pose.bones["head"].matrix_basis.to_quaternion().angle
    assert abs(turned - 0.6981317) < 1e-4, turned
    assert 0.0 < rig.location.x < 4.0 and abs(rig.location.y - 3.0) < 1e-6, tuple(rig.location)
    print(f"unassigned clips: rig kept CHAR3_root, wave strip turned the head {turned:.6f} rad at frame 12")
    print(f"hidden strip warning: {hidden}")
    _check_extrapolation_decides_what_is_hidden(server, rig, target)


def _check_extrapolation_decides_what_is_hidden(server: BlenderMCPServer, rig: bpy.types.Object, target: dict) -> None:
    """
    Measure what Blender plays where the notice says a strip is or is not hidden.

    The root action is keyed over frames 1-24 and the 'hop' strip sits at 30. Held (Blender's
    default) the root plays over the strip; at NOTHING it stops at 24 and the strip plays, and
    the notice must agree. A strip on a higher track at REPLACE then hides 'hop' in turn.

    Args:
        server: The server under test.
        rig: The rig `_check_an_unassigned_clip_plays_from_its_strip` built.
        target: Its animation target.

    Raises:
        AssertionError: When Blender's evaluation and the notice disagree.

    """
    scene = bpy.context.scene
    scene.frame_set(30)
    assert abs(rig.location.x - 4.0) < 1e-6, ("HOLD: the root's last key plays over the strip", tuple(rig.location))

    rig.animation_data.action_extrapolation = "NOTHING"
    scene.frame_set(30)
    assert abs(rig.location.x - 9.0) < 1e-6, ("NOTHING: the hop strip plays", tuple(rig.location))
    rekeyed = _run(
        server,
        "keyframe_object_transform",
        keyframes=[{"object_name": rig.name, "frame": 24, "space": "LOCAL", "location": [4.0, 3.0, 0.0]}],
        action_name="CHAR3_root",
    )
    assert not _hidden_strip_notices(rekeyed), rekeyed

    _run(
        server,
        "keyframe_object_transform",
        keyframes=[{"object_name": rig.name, "frame": 30, "space": "LOCAL", "location": [-5.0, 3.0, 0.0]}],
        action_name="CHAR3_slide",
        assign_action=False,
    )
    _run(server, "manage_nla_tracks", target=target, action="CREATE_TRACK", track_name="Overlay")
    slide_strip = _run(
        server,
        "manage_nla_tracks",
        target=target,
        action="ADD_STRIP",
        track_name="Overlay",
        strip_name="slide",
        action_name="CHAR3_slide",
        frame_start=30,
    )
    (over,) = _hidden_strip_notices(slide_strip)
    assert "plays strip 'slide'" in over and "'hop'" in over and "manage_nla_tracks PATCH_STRIP" in over, over
    scene.frame_set(30)
    assert abs(rig.location.x + 5.0) < 1e-6, ("the upper strip replaces the hop", tuple(rig.location))
    print(f"extrapolation: NOTHING lets the hop play at 30; strip over strip: {over}")


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

    _check_two_characters_keep_their_own_animation(server)
    _check_an_unassigned_clip_plays_from_its_strip(server)

    print("ANIMATION_ROUNDTRIP_SMOKE_OK")


if __name__ == "__main__":
    main()
