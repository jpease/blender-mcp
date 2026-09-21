"""Server-boundary and dispatch coverage for camera tools."""

import asyncio
import math
import sys
import types

import pytest

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError
from test_mutation_transaction import _load_addon

from blender_mcp.server.tools import _dispatch, camera

CAMERA_COMMANDS = {
    "get_camera_rig_info",
    "create_camera",
    "configure_camera",
    "set_scene_camera",
    "point_camera_at",
    "create_camera_target",
    "frame_camera_on_objects",
    "create_orbit_camera_rig",
    "create_dolly_camera_rig",
    "create_crane_camera_rig",
    "create_camera_path_rig",
    "configure_camera_dof",
}

CAMERA_EXTENDED_COMMANDS = {
    "keyframe_camera_rig",
    "set_camera_interpolation",
    "create_focus_pull",
    "create_dolly_zoom",
    "add_camera_shake",
    "create_camera_markers",
    "match_camera_transform",
    "duplicate_camera_rig",
    "add_camera_constraint",
    "configure_camera_render_gate",
    "validate_camera_rig",
}


class _StubConnection:
    def __init__(self, result=None) -> None:
        self.result = result or {"status": "ok"}
        self.calls = []

    def send_command(self, command, params):
        self.calls.append((command, params))
        return self.result


def _run(function, **kwargs):
    return asyncio.run(function(ctx=None, **kwargs))


def test_all_camera_commands_are_public() -> None:
    assert all(callable(getattr(camera, name)) for name in CAMERA_COMMANDS)


def test_all_extended_camera_commands_are_public() -> None:
    assert all(callable(getattr(camera, name)) for name in CAMERA_EXTENDED_COMMANDS)


def test_camera_patch_forbids_unknown_rna_and_invalid_optics() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        camera.CameraOpticsPatch(arbitrary_rna=1)  # pyright: ignore[reportCallIssue]
    with pytest.raises(ValidationError):
        camera.CameraOpticsPatch(lens=0)
    with pytest.raises(ValidationError, match="clip_start must be less"):
        camera.CameraOpticsPatch(clip_start=10, clip_end=1)


def test_create_camera_rejects_ambiguous_orientation_before_dispatch(monkeypatch) -> None:
    connection = _StubConnection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    with pytest.raises(ToolError, match="only one orientation source"):
        _run(
            camera.create_camera,
            scene_name="Scene",
            collection_name="Cameras",
            name="Hero",
            rotation_euler=(0, 0, 0),
            look_at_point=(0, 0, 0),
        )

    assert connection.calls == []


def test_point_camera_at_preflights_target_source_before_dispatch(monkeypatch) -> None:
    connection = _StubConnection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    with pytest.raises(ToolError, match="exactly one of target_object_name or target_point"):
        _run(
            camera.point_camera_at,
            scene_name="Scene",
            camera_name="Hero",
            target_object_name="Target",
            target_point=(0, 0, 0),
        )
    with pytest.raises(ToolError, match="subtarget requires target_object_name"):
        _run(
            camera.point_camera_at,
            scene_name="Scene",
            camera_name="Hero",
            target_point=(0, 0, 0),
            subtarget="Head",
        )

    assert connection.calls == []


def test_point_camera_at_places_before_aiming_and_refuses_a_coincident_placement(monkeypatch) -> None:
    connection = _StubConnection({"camera": "Hero"})
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    _run(
        camera.point_camera_at,
        scene_name="Scene",
        camera_name="Hero",
        target_point=(0.0, 0.0, 1.6),
        camera_location=(0.0, -6.0, 1.7),
    )

    assert connection.calls[0][1]["camera_location"] == (0.0, -6.0, 1.7)

    with pytest.raises(ToolError, match="same point as target_point"):
        _run(
            camera.point_camera_at,
            scene_name="Scene",
            camera_name="Hero",
            target_point=(1.0, 2.0, 3.0),
            camera_location=(1.0, 2.0, 3.0),
        )

    assert len(connection.calls) == 1


def test_configure_camera_serializes_only_explicit_patch_fields(monkeypatch) -> None:
    connection = _StubConnection({"camera": "Hero", "changed_resources": ["Hero Data"]})
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    result = _run(
        camera.configure_camera,
        camera_name="Hero",
        optics=camera.CameraOpticsPatch(lens=85),
        display=camera.CameraDisplayPatch(show_composition_thirds=True),
    )

    assert connection.calls == [
        (
            "configure_camera",
            {
                "camera_name": "Hero",
                "optics": {"lens": 85.0},
                "display": {"show_composition_thirds": True},
            },
        )
    ]
    assert result["changed_objects"] == ["Hero"]
    assert result["changed_resources"] == ["Hero Data"]


def test_rig_builder_defaults_are_plain_values_and_context_is_not_forwarded(monkeypatch) -> None:
    connection = _StubConnection({"changed_objects": ["Orbit Root", "Orbit Camera"]})
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    _run(
        camera.create_orbit_camera_rig,
        scene_name="Scene",
        collection_name="Camera Rigs",
        rig_name="Orbit",
        pivot=(0, 0, 0),
        radius=8,
    )

    command, params = connection.calls[0]
    assert command == "create_orbit_camera_rig"
    assert "ctx" not in params
    assert params["lens"] == pytest.approx(50.0)
    assert params["azimuth"] == pytest.approx(0.0)


def test_path_tool_preflights_path_and_frame_intent(monkeypatch) -> None:
    connection = _StubConnection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    with pytest.raises(ToolError, match="exactly one"):
        _run(
            camera.create_camera_path_rig,
            scene_name="Scene",
            collection_name="Rigs",
            rig_name="Move",
            camera_name="Hero",
        )
    with pytest.raises(ToolError, match="start_frame and end_frame"):
        _run(
            camera.create_camera_path_rig,
            scene_name="Scene",
            collection_name="Rigs",
            rig_name="Move",
            camera_name="Hero",
            path_points=[(0, 0, 0), (1, 0, 0)],
            start_frame=1,
        )

    assert connection.calls == []


def test_dispatch_advertises_all_camera_commands_and_only_inspection_is_read_only(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()

    commands = server._build_command_handlers()

    assert CAMERA_COMMANDS.issubset(commands)
    assert server.command_spec("get_camera_rig_info").read_only
    assert not {name for name in CAMERA_COMMANDS - {"get_camera_rig_info"} if server.command_spec(name).read_only}


def test_dispatch_advertises_extended_commands_and_validation_is_read_only(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()

    commands = server._build_command_handlers()

    assert CAMERA_EXTENDED_COMMANDS.issubset(commands)
    assert server.command_spec("validate_camera_rig").read_only
    assert not {
        name for name in CAMERA_EXTENDED_COMMANDS - {"validate_camera_rig"} if server.command_spec(name).read_only
    }


def test_extended_keyframes_serialize_strict_records(monkeypatch) -> None:
    connection = _StubConnection({"changed_objects": ["Hero"]})
    # Every tool in every package resolves its connection here, so one patch covers all of them.
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    result = _run(
        camera.keyframe_camera_rig,
        keyframes=[
            camera.CameraKeyframe(
                object_name="Hero",
                owner="CAMERA_DATA",
                data_path="lens",
                value=85,
                frame=12,
            )
        ],
        interpolation="LINEAR",
    )

    assert connection.calls == [
        (
            "keyframe_camera_rig",
            {
                "keyframes": [
                    {
                        "object_name": "Hero",
                        "owner": "CAMERA_DATA",
                        "data_path": "lens",
                        "value": 85.0,
                        "frame": 12,
                    }
                ],
                "policy": "REPLACE",
                "interpolation": "LINEAR",
                "handle_left": "AUTO_CLAMPED",
                "handle_right": "AUTO_CLAMPED",
            },
        )
    ]
    assert result["changed_objects"] == ["Hero"]


def test_extended_preflights_ambiguous_subjects_and_marker_actions(monkeypatch) -> None:
    connection = _StubConnection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    with pytest.raises(ToolError, match="exactly one subject"):
        _run(
            camera.create_dolly_zoom,
            scene_name="Scene",
            camera_name="Hero",
            movement_object_name="Dolly",
            start_frame=1,
            end_frame=20,
            start_distance=5,
            end_distance=10,
        )
    with pytest.raises(ToolError, match="markers must not be empty"):
        _run(camera.create_camera_markers, scene_name="Scene", action="CREATE")
    with pytest.raises(ToolError, match="LIST does not accept"):
        _run(
            camera.create_camera_markers,
            scene_name="Scene",
            action="LIST",
            markers=[camera.MarkerEdit(name="Shot", frame=1, camera_name="Hero")],
        )

    assert connection.calls == []


def test_extended_strict_models_validate_ranges_and_constraint_intent() -> None:
    with pytest.raises(ValidationError):
        camera.RenderBorderPatch(min_x=0.8, max_x=0.2)
    with pytest.raises(ValidationError):
        camera.SafeAreasPatch(title=(0.9, 1.1))
    with pytest.raises(ValidationError, match="constraint_name"):
        camera.CameraKeyframe(
            object_name="Hero",
            owner="CONSTRAINT",
            data_path="influence",
            value=1,
            frame=1,
        )


def test_marker_list_dispatch_is_read_only(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()
    monkeypatch.setattr(
        server,
        "_build_command_handlers",
        lambda: {"create_camera_markers": lambda **_params: {"action": "LIST", "camera_cuts": []}},
    )

    response = server.execute_command_internal(
        {"type": "create_camera_markers", "params": {"scene_name": "Scene", "action": "LIST", "markers": []}}
    )

    assert response == {"status": "success", "result": {"action": "LIST", "camera_cuts": []}}


def _marker_handler(monkeypatch):
    """Build the two cameras and the marker list that are all the marker handlers touch."""

    class Markers(list):
        """The slice of `scene.timeline_markers` the marker handlers use."""

        def get(self, name):
            """Return the marker of that name, as Blender's collection does."""
            return next((marker for marker in self if marker.name == name), None)

        def new(self, name, frame):
            """Append a marker carrying no camera yet, as Blender's collection does."""
            marker = types.SimpleNamespace(name=name, frame=frame, camera=None)
            self.append(marker)
            return marker

        def remove(self, marker):
            """Drop the marker, as Blender's collection does."""
            list.remove(self, marker)

    cameras = {
        name: types.SimpleNamespace(name=name, type="CAMERA", data=f"{name} Data") for name in ("WideCam", "TightCam")
    }
    scene = types.SimpleNamespace(
        name="Scene", frame_start=1, camera=cameras["WideCam"], objects=cameras, timeline_markers=Markers()
    )
    addon, _bpy = _load_addon(monkeypatch, data={"scenes": {"Scene": scene}, "objects": cameras})
    return sys.modules[f"{addon.__name__}.handlers.camera"].CameraHandlersMixin(), scene


def test_camera_markers_warn_that_the_earliest_marker_claims_every_frame_before_it(monkeypatch) -> None:
    """Blender binds an unmarked frame to the earliest marker's camera, so one marker is no cut at all."""
    handler, _scene = _marker_handler(monkeypatch)

    late = handler.create_camera_markers(
        "Scene", "CREATE", [{"name": "sh030", "frame": 167, "camera_name": "TightCam"}]
    )

    assert len(late["warnings"]) == 1
    warning = late["warnings"][0]
    assert "'sh030' at frame 167" in warning
    assert "frames 1-166 render through 'TightCam'" in warning
    assert "add one at frame 1" in warning

    opened = handler.create_camera_markers("Scene", "CREATE", [{"name": "sh010", "frame": 1, "camera_name": "WideCam"}])

    assert opened["warnings"] == []


def test_setting_the_scene_camera_reports_the_retroactive_binding_in_the_same_words(monkeypatch) -> None:
    """Assigning scene.camera changes nothing for the frames the earliest marker already claims."""
    handler, _scene = _marker_handler(monkeypatch)
    handler.create_camera_markers("Scene", "CREATE", [{"name": "sh030", "frame": 167, "camera_name": "TightCam"}])

    assigned = handler.set_scene_camera("Scene", "WideCam")

    assert assigned["warnings"] == handler.create_camera_markers("Scene", "LIST")["warnings"]
    assert assigned["warnings"] != []


def test_handler_camera_patch_rolls_back_assignments(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    handler = sys.modules[f"{addon.__name__}.handlers.camera._shared"]

    class Owner:
        lens = 50.0
        shift_x = 0.0

        def __setattr__(self, name, value) -> None:
            if name == "shift_x" and math.isclose(value, 2.0):
                raise RuntimeError("assignment failed")
            object.__setattr__(self, name, value)

    owner = Owner()
    with pytest.raises(RuntimeError, match="assignment failed"):
        handler._patch_values(owner, {"lens": 85.0, "shift_x": 2.0}, {"lens", "shift_x"})

    assert owner.lens == pytest.approx(50.0)
    assert owner.shift_x == pytest.approx(0.0)


def test_handler_binary_solver_finds_smallest_fitting_value(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    handler = sys.modules[f"{addon.__name__}.handlers.camera.targeting"]

    solved = handler._binary_smallest_fit(lambda value: value >= 7.5, 0.0, 1.0)

    assert solved == pytest.approx(7.5)


def test_handler_point_camera_at_rejects_a_placement_on_the_aim_point(monkeypatch) -> None:
    class Vector(tuple):
        """The slice of mathutils.Vector the placement guard uses before any transform is read."""

        def __sub__(self, other):
            return Vector(mine - theirs for mine, theirs in zip(self, other, strict=True))

        @property
        def length_squared(self):
            return sum(component * component for component in self)

    class Camera:
        """A camera whose transform cannot be touched: reading it at all means the guard ran late."""

        name = "Hero"
        type = "CAMERA"
        data = "Hero Data"

        @property
        def matrix_world(self):
            raise AssertionError("the camera was moved before the coincident placement was rejected")

    subject = Camera()
    scene = types.SimpleNamespace(name="Scene", objects={"Hero": subject})
    addon, _bpy = _load_addon(monkeypatch, data={"scenes": {"Scene": scene}, "objects": {"Hero": subject}})
    monkeypatch.setattr(sys.modules["mathutils"], "Vector", Vector, raising=False)
    handler = sys.modules[f"{addon.__name__}.handlers.camera.targeting"]._TargetingMixin()

    with pytest.raises(ValueError, match=r"\[1.0, 2.0, 3.0\] and the aim target \[1.0, 2.0, 3.0\]"):
        handler.point_camera_at("Scene", "Hero", target_point=(1.0, 2.0, 3.0), camera_location=(1.0, 2.0, 3.0))


def test_camera_keyframe_requires_exactly_one_timing_source() -> None:
    with pytest.raises(ValidationError, match="exactly one of frame or at_seconds"):
        camera.CameraKeyframe(
            object_name="Hero",
            owner="CAMERA_DATA",
            data_path="lens",
            value=85,
        )
    with pytest.raises(ValidationError, match="exactly one of frame or at_seconds"):
        camera.CameraKeyframe(
            object_name="Hero",
            owner="CAMERA_DATA",
            data_path="lens",
            value=85,
            frame=12,
            at_seconds=0.5,
        )


def test_focus_pull_requires_exactly_one_timing_source(monkeypatch) -> None:
    connection = _StubConnection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    with pytest.raises(ToolError, match="supply exactly one of start_frame or start_at_seconds"):
        _run(
            camera.create_focus_pull,
            scene_name="Scene",
            camera_name="Hero",
            start_frame=None,
            start_at_seconds=None,
            end_frame=20,
            start_subject_name="Target",
        )
    with pytest.raises(ToolError, match="supply exactly one of end_frame or end_at_seconds"):
        _run(
            camera.create_focus_pull,
            scene_name="Scene",
            camera_name="Hero",
            start_frame=1,
            end_frame=None,
            end_at_seconds=None,
            start_subject_name="Target",
            end_subject_name="Target2",
        )


def test_dolly_zoom_requires_exactly_one_timing_source(monkeypatch) -> None:
    connection = _StubConnection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    with pytest.raises(ToolError, match="supply exactly one of start_frame or start_at_seconds"):
        _run(
            camera.create_dolly_zoom,
            scene_name="Scene",
            camera_name="Hero",
            movement_object_name="Dolly",
            start_frame=None,
            start_at_seconds=None,
            end_frame=20,
            start_distance=5,
            end_distance=10,
            subject_object_name="Target",
        )
    with pytest.raises(ToolError, match="supply exactly one of end_frame or end_at_seconds"):
        _run(
            camera.create_dolly_zoom,
            scene_name="Scene",
            camera_name="Hero",
            movement_object_name="Dolly",
            start_frame=1,
            end_frame=None,
            end_at_seconds=None,
            start_distance=5,
            end_distance=10,
            subject_object_name="Target",
        )
    with pytest.raises(ToolError, match="start_distance and end_distance must be provided"):
        _run(
            camera.create_dolly_zoom,
            scene_name="Scene",
            camera_name="Hero",
            movement_object_name="Dolly",
            start_frame=1,
            end_frame=20,
            subject_object_name="Target",
        )


def test_add_camera_shake_requires_exactly_one_timing_source(monkeypatch) -> None:
    connection = _StubConnection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    with pytest.raises(ToolError, match="supply exactly one of frame_start or frame_start_at_seconds"):
        _run(
            camera.add_camera_shake,
            scene_name="Scene",
            camera_name="Hero",
            collection_name="Rigs",
            control_name="Shake",
            frame_start=None,
            frame_start_at_seconds=None,
            frame_end=20,
        )
    with pytest.raises(ToolError, match="supply exactly one of frame_end or frame_end_at_seconds"):
        _run(
            camera.add_camera_shake,
            scene_name="Scene",
            camera_name="Hero",
            collection_name="Rigs",
            control_name="Shake",
            frame_start=1,
            frame_end=None,
            frame_end_at_seconds=None,
        )
