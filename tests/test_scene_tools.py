"""Regression coverage for declarative scene composition tools."""

import asyncio

import pytest

from pydantic import ValidationError
from test_mutation_transaction import _load_addon

from blender_mcp.server.tools import scene, scene_authoring

SCENE_COMMANDS = {
    "create_geometry_object",
    "set_object_transform",
    "duplicate_or_instance_objects",
    "manage_scene_collections",
    "manage_object_hierarchy",
    "manage_object_constraints",
    "manage_modifiers",
    "remove_scene_objects",
    "reset_scene",
}


class _Connection:
    def __init__(self) -> None:
        self.calls = []

    def send_command(self, command, params):
        self.calls.append((command, params))
        return {"changed_objects": [params.get("name", "Created")], "kind": params.get("geometry", {}).get("kind")}


def test_scene_tools_are_registered_and_dispatched(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})

    # `scene` and `scene_authoring` register onto the same FastMCP app; importing both above is
    # what a `scene-authoring` process does, and all nine commands must still be reachable.
    assert SCENE_COMMANDS <= set(scene.mcp._tool_manager._tools)
    assert SCENE_COMMANDS <= set(addon.BlenderMCPServer()._build_command_handlers())
    assert not SCENE_COMMANDS & addon.BlenderMCPServer._READ_ONLY_COMMANDS


def test_create_geometry_object_serializes_discriminated_geometry(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(scene, "get_blender_connection", lambda: connection)
    geometry = scene_authoring.MeshGeometry(
        vertices=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        faces=[[0, 1, 2]],
    )

    result = asyncio.run(scene_authoring.create_geometry_object(ctx=None, name="Triangle", geometry=geometry))

    assert connection.calls[0][0] == "create_geometry_object"
    assert connection.calls[0][1]["geometry"]["kind"] == "MESH"
    assert result["changed_objects"] == ["Triangle"]


def test_reset_scene_requires_explicit_confirmation(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(scene, "get_blender_connection", lambda: connection)

    with pytest.raises(ValueError, match="confirm_reset=True is required"):
        asyncio.run(scene_authoring.reset_scene(ctx=None))

    assert connection.calls == []


def test_reset_scene_dispatches_with_confirmation(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(scene, "get_blender_connection", lambda: connection)

    asyncio.run(
        scene_authoring.reset_scene(ctx=None, confirm_reset=True, scene_name="Scene", purge_orphaned_data=False)
    )

    assert connection.calls[0] == (
        "reset_scene",
        {"confirm_reset": True, "scene_name": "Scene", "purge_orphaned_data": False},
    )


def test_scene_models_reject_ambiguous_or_degenerate_transforms() -> None:
    with pytest.raises(ValidationError, match="at least one transform"):
        scene.TransformPatch()
    with pytest.raises(ValidationError, match="at most one rotation"):
        scene.TransformPatch(rotation_euler=(0, 0, 0), rotation_quaternion=(1, 0, 0, 0))
    with pytest.raises(ValidationError, match="non-zero"):
        scene.TransformPatch(scale=(1, 0, 1))
    with pytest.raises(ValidationError, match="non-zero"):
        scene.InstanceTransform(scale=(1, 1, 0))


def test_point_cloud_requires_one_radius_per_point() -> None:
    with pytest.raises(ValidationError, match="one value per point"):
        scene_authoring.PointCloudGeometry(points=[(0, 0, 0), (1, 0, 0)], radii=[0.5])


def test_modern_curves_validate_offsets_and_attribute_domains() -> None:
    geometry = scene_authoring.CurvesGeometry(
        points=[(0, 0, 0), (0, 0, 1), (1, 0, 0)],
        curve_sizes=[2, 1],
        cyclic=[False, True],
        attributes=[
            scene_authoring.GeometryAttribute(name="density", data_type="FLOAT", domain="CURVE", values=[0.5, 1.0])
        ],
    )
    assert geometry.curve_sizes == [2, 1]
    with pytest.raises(ValidationError, match="sum to the number of points"):
        scene_authoring.CurvesGeometry(points=[(0, 0, 0)], curve_sizes=[2])


def test_legacy_curve_points_and_surface_dimensions_are_typed() -> None:
    point = scene_authoring.CurvePoint(
        co=(1, 2, 3),
        radius=0.5,
        tilt=0.25,
        handle_left=(0, 2, 3),
        handle_right=(2, 2, 3),
    )
    spline = scene_authoring.SplineRecord(type="BEZIER", points=[point])
    assert spline.points[0].radius == 0.5
    with pytest.raises(ValidationError, match="point_count_u"):
        scene_authoring.SplineRecord(type="NURBS", points=[(0, 0, 0), (1, 0, 0)], point_count_u=2, point_count_v=2)


def test_modifier_schema_is_discriminated_and_rejects_wrong_settings() -> None:
    from pydantic import TypeAdapter

    schema = TypeAdapter(scene.ModifierSpecInput).json_schema()
    assert len(schema["oneOf"]) == 30
    screw = TypeAdapter(scene.ModifierSpecInput).validate_python(
        {"name": "Thread", "type": "SCREW", "settings": {"steps": 16, "angle": 6.28}}
    )
    assert screw.settings.steps == 16
    with pytest.raises(ValidationError):
        TypeAdapter(scene.ModifierSpecInput).validate_python(
            {"name": "Thread", "type": "SCREW", "settings": {"unknown": 1}}
        )


def test_breaking_tool_names_are_absent() -> None:
    registered = set(scene.mcp._tool_manager._tools)
    removed = {
        "execute_blender_code",
        "viewport_overlay_toggle",
        "search_polyhaven_assets",
        "download_sketchfab_model",
        "model_mirror",
        "model_array",
        "model_radial_array",
    }

    assert not removed & registered
