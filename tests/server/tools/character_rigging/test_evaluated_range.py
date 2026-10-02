"""
Regression coverage for `sample_evaluated_range`'s refusals, paging and timeline restore.

Every refusal is asserted to land before the playhead moves: a sampler that steps the timeline
and then rejects its own arguments has already re-evaluated the scene for nothing and, on a
failure past the first frame, left it somewhere else. The geometry itself - ray parity against a
real closed mesh, a real armature's evaluated bones - is proven by
`tests/blender_evaluated_range_smoke.py` against Blender.
"""

import sys
import types

import pytest

from conftest import load_addon

from .rig_doubles import _Matrix, _SceneObjects, _Vector


def _armature(name, bones):
    """
    Build an armature whose evaluated copy rises one metre per frame, so each frame reads differently.

    Args:
        name: Object name.
        bones: Bone name to `(head, tail)` in armature space.

    Returns:
        types.SimpleNamespace: The armature object.

    """
    pose = types.SimpleNamespace(bones={})
    for bone, (head, tail) in bones.items():
        pose.bones[bone] = types.SimpleNamespace(head=_Vector(head), tail=_Vector(tail))
    armature = types.SimpleNamespace(name=name, type="ARMATURE", data=object(), pose=pose)
    armature.evaluated_get = lambda depsgraph: types.SimpleNamespace(
        pose=pose, matrix_world=_Matrix.Translation((0.0, 0.0, float(depsgraph.frame)))
    )
    return armature


def _scene_with(monkeypatch, *objects):
    """
    Load the add-on against the given objects, the playhead parked mid-frame.

    Args:
        monkeypatch: pytest's patcher, owned by the caller's test.
        *objects: Scene objects.

    Returns:
        tuple: The server, the fake scene, and the list of `frame_set` calls made.

    """
    addon, bpy = load_addon(monkeypatch, data={"objects": _SceneObjects({obj.name: obj for obj in objects})})
    scene = bpy.context.scene
    scene.frame_current = 7
    scene.frame_subframe = 0.25
    calls = []

    def frame_set(frame, subframe=0.0):
        calls.append((frame, subframe))
        scene.frame_current = frame
        scene.frame_subframe = subframe

    scene.frame_set = frame_set
    bpy.context.evaluated_depsgraph_get = lambda: types.SimpleNamespace(frame=scene.frame_current)
    mathutils = sys.modules["mathutils"]
    monkeypatch.setattr(mathutils, "Matrix", _Matrix, raising=False)
    monkeypatch.setattr(mathutils, "Vector", _Vector, raising=False)
    return addon.BlenderMCPServer(), scene, calls


_BONES = {"lower": ((0.0, 0.0, 0.0), (0.0, 0.0, 1.0)), "upper": ((0.0, 0.0, 1.0), (0.0, 1.0, 1.0))}
_UPPER = [{"armature_object_name": "Rig", "bone_name": "upper"}]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"frames": [1, 2], "frame_start": 1, "frame_end": 2}, "exactly one of frames or frame_start/frame_end"),
        ({}, "exactly one of frames or frame_start/frame_end"),
        ({"frame_start": 1}, "frame_start and frame_end must be supplied together"),
        ({"frame_start": 10, "frame_end": 1}, "frame_end must not be before frame_start"),
        ({"frames": [3, 3]}, "Duplicate frames: 3"),
        ({"frames": list(range(251))}, "251 frames; at most 250"),
        # 1..300 at step 1 is 300 frames; step 2 is 150, the smallest step that fits.
        (
            {"frame_start": 1, "frame_end": 300},
            "300 frames; at most 250 are sampled per call. Raise frame_step to at least 2",
        ),
        ({"frames": [1], "bone_points": [{"armature_object_name": "Rig", "bone_name": "missing"}]}, "bone 'missing'"),
        ({"frames": [1], "bone_points": [{"armature_object_name": "Ghost", "bone_name": "upper"}]}, "Object not found"),
        ({"frames": [1], "bone_points": _UPPER * 2}, "Duplicate bone_points"),
        ({"frames": [1], "mesh_metrics": {"object_names": ["Rig"]}}, "'Rig' is not a mesh"),
        ({"frames": [1], "mesh_metrics": {"object_names": ["Floor"], "against_object_names": ["Rig"]}}, "not a mesh"),
        ({"frames": [1]}, "Name something to sample"),
    ],
)
def test_a_refused_request_never_moves_the_playhead(monkeypatch, arguments, message) -> None:
    """Every argument and every name is checked before the first frame is evaluated."""
    floor = types.SimpleNamespace(name="Floor", type="MESH", data=object())
    server, scene, calls = _scene_with(monkeypatch, _armature("Rig", _BONES), floor)

    with pytest.raises(ValueError, match=message):
        server.sample_evaluated_range(**arguments)

    assert calls == []
    assert (scene.frame_current, scene.frame_subframe) == (7, 0.25)


def test_a_page_evaluates_only_its_own_frames_and_restores_the_subframe(monkeypatch) -> None:
    """Paging must not evaluate the frames it does not return, and must put the subframe back."""
    server, scene, calls = _scene_with(monkeypatch, _armature("Rig", _BONES))

    reply = server.sample_evaluated_range(
        frame_start=10, frame_end=18, frame_step=2, bone_points=[*_UPPER], limit=2, offset=1
    )

    assert [item["frame"] for item in reply["samples"]["items"]] == [12, 14]
    assert calls == [(12, 0.0), (14, 0.0), (7, 0.25)]
    assert (scene.frame_current, scene.frame_subframe) == (7, 0.25)
    assert reply["timeline_restored"] == {"frame": 7, "subframe": 0.25}
    assert reply["samples"]["total"] == 5
    assert reply["samples"]["truncated"] is True
    assert reply["samples"]["next_offset"] == 3
    # The evaluated armature sits at z = frame, so the tail moves with the frame it was read at.
    assert reply["samples"]["items"][1]["bones"] == [{"head_world": [0.0, 0.0, 15.0], "tail_world": [0.0, 1.0, 15.0]}]
    assert reply["bone_points"] == _UPPER


def test_a_failure_mid_range_still_restores_the_playhead(monkeypatch) -> None:
    """A frame that fails to evaluate must not strand the timeline on it."""
    armature = _armature("Rig", _BONES)
    server, scene, calls = _scene_with(monkeypatch, armature)

    def failing(depsgraph):
        raise RuntimeError(f"evaluation failed at {depsgraph.frame}")

    armature.evaluated_get = failing

    with pytest.raises(RuntimeError, match="evaluation failed at 1"):
        server.sample_evaluated_range(frames=[1, 2], bone_points=_UPPER)

    assert calls[-1] == (7, 0.25)
    assert (scene.frame_current, scene.frame_subframe) == (7, 0.25)
