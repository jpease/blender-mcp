r"""
Prove `create_camera(from_viewport=True)` reproduces the live 3D viewport against a GUI Blender.

`--background` has no window, so `just smoke` can only prove the refusal; the claim itself -
that the new camera sees what the viewport sees - needs a real `RegionView3D`. The companion
`in_blender_viewport_camera.py` poses the largest viewport (a known view matrix, lens and
distance; the region is whatever size the window gave it, and the scene's render resolution is
set to that size so the camera frame has the region's aspect), then projects world points both
ways. For each of a perspective, an orthographic and a camera view this scenario checks:

- the camera's `matrix_world` equals the viewport's `view_matrix.inverted()`;
- every point lands at the same normalized position through `world_to_camera_view` (the
  camera) as through `location_3d_to_region_2d` divided by the region size (the viewport).

Run it:

    python scripts/blender_rig.py --work-dir <dir> \
        --scenario scripts/rig_scenarios/scenario_viewport_camera.py \
        --blender-script scripts/rig_scenarios/in_blender_viewport_camera.py

or, as the permanent gate that skips itself without a live Blender:

    just gate
"""

import json
import math
import time

from pathlib import Path
from typing import Protocol


class Rig(Protocol):
    """The part of `BlenderRig` this scenario uses (a Protocol: the rig loads scenarios by path)."""

    work_dir: Path

    def send(self, command_type: str, params: dict | None = None) -> dict:
        """
        Send one addon command and return its decoded response.

        Args:
            command_type: The addon command name.
            params: Command parameters; omitted means none.

        Returns:
            dict: The decoded response.

        """
        ...


_COLLECTION = "RigViewportCameras"
_EYE = (7.0, -6.0, 4.0)
_LENS = 35.0
_DISTANCE = 9.0
# Spread across the frame and in depth, so a wrong lens, sensor, aspect or ortho scale moves
# at least one of them visibly.
_POINTS = ((0.0, 0.0, 0.0), (1.0, 0.5, -0.3), (-1.2, 0.8, 0.6), (0.4, -1.0, 1.0), (-0.5, -0.7, -0.9))
# Normalized-frame tolerance: 0.1 % of the frame, far below any lens or sensor mistake.
_NDC_TOLERANCE = 1e-3
_MATRIX_TOLERANCE = 1e-4
_REPLY_TIMEOUT_SECONDS = 30.0


def _look_at_view_matrix(eye: tuple[float, float, float]) -> list[list[float]]:
    """
    Build the view matrix of an eye looking at the origin with world +Z up.

    Args:
        eye: World-space eye position.

    Returns:
        list[list[float]]: The 4x4 world-to-view matrix, rows.

    """
    forward = [-c for c in eye]
    length = math.sqrt(sum(c * c for c in forward))
    forward = [c / length for c in forward]
    right = [forward[1] * 1.0 - forward[2] * 0.0, forward[2] * 0.0 - forward[0] * 1.0, 0.0]
    length = math.sqrt(sum(c * c for c in right))
    right = [c / length for c in right]
    up = [
        right[1] * forward[2] - right[2] * forward[1],
        right[2] * forward[0] - right[0] * forward[2],
        right[0] * forward[1] - right[1] * forward[0],
    ]
    back = [-c for c in forward]
    rows = [right, up, back]
    return [[*row, -sum(row[i] * eye[i] for i in range(3))] for row in rows] + [[0.0, 0.0, 0.0, 1.0]]


def _ask(rig: Rig, request_name: str, result_name: str, payload: dict) -> dict:
    """
    Hand the in-Blender helper one request and wait for its answer.

    Args:
        rig: The live rig.
        request_name: File the helper polls for.
        result_name: File the helper answers in.
        payload: The request.

    Returns:
        dict: The helper's answer.

    Raises:
        SystemExit: If the helper never answered or reported an error.

    """
    result_path = rig.work_dir / result_name
    result_path.unlink(missing_ok=True)
    pending = rig.work_dir / f"{request_name}.tmp"
    pending.write_text(json.dumps(payload), encoding="utf-8")
    pending.rename(rig.work_dir / request_name)
    deadline = time.monotonic() + _REPLY_TIMEOUT_SECONDS
    while not result_path.is_file():
        if time.monotonic() > deadline:
            raise SystemExit(f"the in-Blender helper never answered {request_name}; was it passed as --blender-script?")
        time.sleep(0.05)
    time.sleep(0.05)
    answer = json.loads(result_path.read_text(encoding="utf-8"))
    if not answer["ok"]:
        raise SystemExit(f"the in-Blender helper failed {request_name}: {answer['error']}")
    return answer


def _create_from_viewport(rig: Rig, scene_name: str, name: str) -> dict:
    """
    Create one camera from the live viewport and return its result.

    Args:
        rig: The live rig.
        scene_name: The scene to create it in.
        name: The camera's name.

    Returns:
        dict: The command's result.

    Raises:
        SystemExit: If the command failed.

    """
    response = rig.send(
        "create_camera",
        {"scene_name": scene_name, "collection_name": _COLLECTION, "name": name, "from_viewport": True},
    )
    if response["status"] != "success":
        raise SystemExit(f"create_camera(from_viewport=True) {name!r} failed: {response}")
    return response["result"]


def _assert_matches_viewport(rig: Rig, camera: str, label: str) -> dict:
    """
    Assert one camera's transform and projection match the posed viewport.

    Args:
        rig: The live rig.
        camera: The camera object to compare.
        label: What view this is, for messages.

    Returns:
        dict: The helper's projection answer.

    """
    answer = _ask(rig, "project_request.json", "project_result.json", {"camera": camera, "points": _POINTS})
    for row, (got, want) in enumerate(zip(answer["matrix_world"], answer["view_matrix_inverted"], strict=True)):
        worst = max(abs(a - b) for a, b in zip(got, want, strict=True))
        assert worst <= _MATRIX_TOLERANCE, f"{label}: matrix_world row {row} is {got}, view_matrix.inverted() is {want}"
    for point, cam, region in zip(_POINTS, answer["camera_ndc"], answer["region_ndc"], strict=True):
        assert region is not None, f"{label}: {point} is behind the viewport; pick points in front of it"
        error = max(abs(cam[0] - region[0]), abs(cam[1] - region[1]))
        print(f"RIG: {label}: {point} camera {cam} viewport {region} (|d|={error:.2e})", flush=True)
        assert error <= _NDC_TOLERANCE, (
            f"{label}: {point} projects to {cam} through the camera but {region} through the viewport"
        )
    return answer


def _check_view(rig: Rig, scene_name: str, mode: str) -> str:
    """
    Pose the viewport in one projection, create a camera from it, and compare them.

    Args:
        rig: The live rig.
        scene_name: The scene to create the camera in.
        mode: PERSP or ORTHO.

    Returns:
        str: The created camera's name.

    """
    view = _ask(
        rig,
        "view_request.json",
        "view_ready.json",
        {"mode": mode, "lens": _LENS, "distance": _DISTANCE, "view_matrix": _look_at_view_matrix(_EYE)},
    )
    assert view["view_perspective"] == mode, view
    print(f"RIG: {mode} viewport region {view['region']}, lens {view['lens']}, distance {view['view_distance']}")
    result = _create_from_viewport(rig, scene_name, f"RigFromViewport{mode.title()}")
    assert result["settings"]["type"] == mode, result["settings"]
    answer = _assert_matches_viewport(rig, result["object"], mode)
    assert answer["camera_type"] == mode, answer
    return result["object"]


def _check_camera_view(rig: Rig, scene_name: str, source: str) -> None:
    """
    Put the viewport in camera view through `source`, and check the copy is that camera's view.

    Args:
        rig: The live rig.
        scene_name: The scene to create the camera in.
        source: The camera the viewport looks through.

    """
    # Optics the viewport itself does not have (its lens is 35 mm over a 72 mm sensor), so a copy
    # built from the viewport's own lens instead of the camera's would show here.
    configured = rig.send("configure_camera", {"camera_name": source, "optics": {"lens": 85.0, "sensor_width": 36.0}})
    assert configured["status"] == "success", configured
    view = _ask(rig, "view_request.json", "view_ready.json", {"mode": "CAMERA", "camera": source})
    assert view["view_perspective"] == "CAMERA", view
    result = _create_from_viewport(rig, scene_name, "RigFromViewportCamera")
    source_answer = _ask(rig, "project_request.json", "project_result.json", {"camera": source, "points": _POINTS})
    copy_answer = _ask(
        rig, "project_request.json", "project_result.json", {"camera": result["object"], "points": _POINTS}
    )
    # A quaternion round trip through matrix_world moves the last float32 bit, so compare within tolerance.
    for got, want in zip(copy_answer["matrix_world"], source_answer["matrix_world"], strict=True):
        assert max(abs(a - b) for a, b in zip(got, want, strict=True)) <= _MATRIX_TOLERANCE, (got, want)
    for got, want in zip(copy_answer["camera_ndc"], source_answer["camera_ndc"], strict=True):
        assert max(abs(a - b) for a, b in zip(got, want, strict=True)) <= _NDC_TOLERANCE, (got, want)
    assert copy_answer["lens"] == source_answer["lens"], (copy_answer, source_answer)
    assert copy_answer["camera_type"] == source_answer["camera_type"], (copy_answer, source_answer)
    print(f"RIG: CAMERA view copied {source} into {result['object']}", flush=True)


def _check_refusals(rig: Rig, scene_name: str) -> None:
    """
    Prove a placement alongside from_viewport is refused live too, and builds nothing.

    Args:
        rig: The live rig.
        scene_name: The scene to try in.

    """
    refused = rig.send(
        "create_camera",
        {
            "scene_name": scene_name,
            "collection_name": _COLLECTION,
            "name": "RigFromViewportRefused",
            "from_viewport": True,
            "target_point": (0.0, 0.0, 0.0),
        },
    )
    assert refused["status"] == "error" and "from_viewport" in refused["message"], refused
    probe = rig.send("get_object_info", {"name": "RigFromViewportRefused"})
    assert probe["status"] == "error", f"a refused from_viewport call left a camera behind: {probe}"


def run(rig: Rig) -> None:
    """
    Drive the whole from-viewport camera gate against a live GUI Blender.

    Args:
        rig: The live rig, already serving on its socket.

    Raises:
        SystemExit: If the scene cannot be read.

    """
    listing = rig.send("list_scene_objects")
    if listing["status"] != "success":
        raise SystemExit(f"list_scene_objects failed: {listing}")
    scene_name = listing["result"]["name"]

    persp = _check_view(rig, scene_name, "PERSP")
    _check_view(rig, scene_name, "ORTHO")
    _check_camera_view(rig, scene_name, persp)
    _check_refusals(rig, scene_name)
