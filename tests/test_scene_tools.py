"""Regression coverage for declarative scene composition tools."""

import asyncio

import pytest

from conftest import StubFactory
from pydantic import ValidationError
from test_mutation_transaction import _load_addon

# Importing these registers their tools onto the process-global FastMCP app for the rest of the
# session. `scene` is a core module the package initializer already imported and enriched;
# `scene_authoring` arrives after `finalize_tool_documentation` has run, so its tools carry raw
# docstrings and no annotations. Any assertion about which tools a *selection* advertises, or
# about descriptions and annotations, must go through test_bundles.py's subprocess helper.
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


def test_scene_tools_are_registered_and_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    """All nine scene commands stay reachable and mutating once both modules are imported."""
    addon, _bpy = _load_addon(monkeypatch, data={})

    # `scene` and `scene_authoring` register onto the same FastMCP app; importing both above is
    # what a `scene-authoring` process does, and all nine commands must still be reachable.
    assert set(scene.mcp._tool_manager._tools) >= SCENE_COMMANDS
    assert set(addon.BlenderMCPServer()._build_command_handlers()) >= SCENE_COMMANDS
    assert not SCENE_COMMANDS & addon.BlenderMCPServer._READ_ONLY_COMMANDS


def test_create_geometry_object_serializes_discriminated_geometry(stub_blender_connection: StubFactory) -> None:
    """A discriminated geometry model reaches Blender with its `kind` tag intact."""
    connection = stub_blender_connection()
    geometry = scene_authoring.MeshGeometry(
        vertices=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        faces=[[0, 1, 2]],
    )

    result = asyncio.run(scene_authoring.create_geometry_object(ctx=None, name="Triangle", geometry=geometry))

    assert connection.calls[0][0] == "create_geometry_object"
    assert connection.calls[0][1]["geometry"]["kind"] == "MESH"
    assert result["changed_objects"] == ["Triangle"]


def test_reset_scene_requires_explicit_confirmation(stub_blender_connection: StubFactory) -> None:
    """reset_scene refuses to dispatch anything without confirm_reset=True."""
    connection = stub_blender_connection()

    with pytest.raises(ValueError, match="confirm_reset=True is required"):
        asyncio.run(scene_authoring.reset_scene(ctx=None))

    assert connection.calls == []


def test_reset_scene_dispatches_with_confirmation(stub_blender_connection: StubFactory) -> None:
    """A confirmed reset forwards every parameter verbatim to the addon."""
    connection = stub_blender_connection()

    asyncio.run(
        scene_authoring.reset_scene(ctx=None, confirm_reset=True, scene_name="Scene", purge_orphaned_data=False)
    )

    assert connection.calls[0] == (
        "reset_scene",
        {"confirm_reset": True, "scene_name": "Scene", "purge_orphaned_data": False},
    )


def test_scene_models_reject_ambiguous_or_degenerate_transforms() -> None:
    """Transform inputs reject ambiguous rotations, bad matrices, and zero scale."""
    with pytest.raises(ValidationError, match="at least one transform"):
        scene.TransformPatch()
    with pytest.raises(ValidationError, match="at most one rotation"):
        scene.TransformPatch(rotation_euler=(0, 0, 0), rotation_quaternion=(1, 0, 0, 0))
    with pytest.raises(ValidationError, match="non-zero"):
        scene.TransformPatch(scale=(1, 0, 1))
    with pytest.raises(ValidationError, match="non-zero"):
        scene.InstanceTransform(scale=(1, 1, 0))


def test_point_cloud_requires_one_radius_per_point() -> None:
    """An optional radii array must run parallel to the point array."""
    with pytest.raises(ValidationError, match="one value per point"):
        scene_authoring.PointCloudGeometry(points=[(0, 0, 0), (1, 0, 0)], radii=[0.5])


def test_modern_curves_validate_offsets_and_attribute_domains() -> None:
    """Curves offsets must sum to the point count and attributes must use valid domains."""
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
    """Legacy curve points keep their typed fields and surfaces need a rectangular U/V shape."""
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
    """The modifier union stays discriminated by type and refuses another variant's settings."""
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
    """Tool names removed in earlier breaking changes must never reappear in the registry."""
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


def test_remove_scene_objects_dispatches_named_objects(stub_blender_connection: StubFactory) -> None:
    """A confirmed removal must reach the addon under its own command name, carrying the names."""
    connection = stub_blender_connection()

    result = asyncio.run(scene_authoring.remove_scene_objects(ctx=None, object_names=["Cube"], confirm_remove=True))

    assert connection.calls[0] == (
        "remove_scene_objects",
        {"object_names": ["Cube"], "managed_rig": None, "confirm_remove": True},
    )
    # No `name` parameter, so the stub falls back to its documented "Created" echo.
    assert result["changed_objects"] == ["Created"]


def test_remove_scene_objects_requires_exactly_one_selector(stub_blender_connection: StubFactory) -> None:
    """Neither selector, or both, is a client error that must not reach Blender."""
    connection = stub_blender_connection()

    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(scene_authoring.remove_scene_objects(ctx=None, confirm_remove=True))

    assert connection.calls == []


def test_attribute_domains_are_rejected_per_geometry_kind() -> None:
    """
    Each geometry accepts only its own attribute domains, and says which in the error.

    Both messages come from the single `_validate_attributes` helper, so they are asserted
    here: the helper derives the allowlist wording from its `counts` mapping, and nothing
    else would notice if a reorder or a reword changed what clients read.
    """
    with pytest.raises(ValidationError, match="only support POINT or CURVE domains"):
        scene_authoring.CurvesGeometry(
            points=[(0, 0, 0)],
            curve_sizes=[1],
            attributes=[scene_authoring.GeometryAttribute(name="bad", data_type="FLOAT", domain="LAYER", values=[1.0])],
        )

    with pytest.raises(ValidationError, match="only support POINT or STROKE domains"):
        scene_authoring.GreasePencilFrame(
            frame_number=1,
            strokes=[scene_authoring.GreasePencilStroke(points=[(0, 0, 0)])],
            attributes=[scene_authoring.GeometryAttribute(name="bad", data_type="FLOAT", domain="CURVE", values=[1.0])],
        )
