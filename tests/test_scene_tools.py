"""Regression coverage for declarative scene composition tools."""

import asyncio
import sys
import types

import pytest

from conftest import StubFactory
from pydantic import ValidationError
from pydantic_core import to_json
from test_mutation_transaction import FakeCollection, _load_addon

# These imports register tools on the process-global FastMCP app for the whole session, and
# `scene_authoring` loads after `finalize_tool_documentation`, so its tools lack annotations.
# Test what a selection advertises, or tool descriptions, via test_bundles.py's subprocess helper.
from blender_mcp.server.tools import scene, scene_authoring
from blender_mcp.server.tools.envelope import REPLY_BYTE_BUDGET, STALE_INDEX_WARNING

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

    # Importing both modules is what a `scene-authoring` process does.
    assert set(scene.mcp._tool_manager._tools) >= SCENE_COMMANDS
    assert set(addon.BlenderMCPServer()._build_command_handlers()) >= SCENE_COMMANDS
    assert not {name for name in SCENE_COMMANDS if addon.BlenderMCPServer.command_spec(name).read_only}


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
    assert spline.points[0].radius == pytest.approx(0.5)
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


def test_an_applied_modifier_reply_fits_the_budget_with_its_stale_index_warning(
    stub_blender_connection: StubFactory,
) -> None:
    """
    APPLY's warning must be in the reply while it is measured, not appended to the finished envelope.

    The stub answers with a page long enough that the envelope has to shorten it, which is the only
    place an appended warning shows: the shortening measured a reply that did not carry it yet, and
    the warning's own 300-odd bytes then pushed the reply back over the budget.
    """
    stub_blender_connection(
        {
            "name": "Hero",
            "applied_modifier": "Subdivision",
            "modifiers": [
                {"name": f"Bevel.{index:03d}", "type": "BEVEL", "show_viewport": True} for index in range(400)
            ],
        }
    )

    result = asyncio.run(
        scene.manage_modifiers(
            ctx=None,
            object_name="Hero",
            action="APPLY",
            modifier={"name": "Subdivision", "type": "SUBSURF"},
            confirm_destructive=True,
        )
    )

    assert STALE_INDEX_WARNING in result["warnings"]
    assert len(result["data"]["modifiers"]) < 400, "the page must have been shortened for this to prove anything"
    assert len(to_json(result, fallback=str, indent=2)) <= REPLY_BYTE_BUDGET


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
        # Renamed on protocol 41: it was never limited to Geometry Nodes, and a posing session
        # looking for an evaluated readback never tried a tool named for a procedural stack.
        "evaluate_procedural_geometry",
    }

    assert not removed & registered


def test_remove_scene_objects_dispatches_named_objects(stub_blender_connection: StubFactory) -> None:
    """A confirmed removal reaches the addon under its own command name, with the names."""
    connection = stub_blender_connection()

    result = asyncio.run(scene.remove_scene_objects(ctx=None, object_names=["Cube"], confirm_remove=True))

    assert connection.calls[0] == (
        "remove_scene_objects",
        {"object_names": ["Cube"], "managed_rig": None, "confirm_remove": True},
    )
    # With no `name` parameter the stub echoes "Created".
    assert result["changed_objects"] == ["Created"]


def test_remove_scene_objects_requires_exactly_one_selector(stub_blender_connection: StubFactory) -> None:
    """Neither selector, or both, is a client error that must not reach Blender."""
    connection = stub_blender_connection()

    with pytest.raises(ValueError, match="exactly one"):
        asyncio.run(scene.remove_scene_objects(ctx=None, confirm_remove=True))

    assert connection.calls == []


def test_attribute_domains_are_rejected_per_geometry_kind() -> None:
    """
    Each geometry accepts only its own attribute domains, and says which in the error.

    The wording is built from `_validate_attributes`'s `counts` mapping, so a reorder there
    changes what clients read.
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


class _Matrix4:
    """A translation-only 4x4, with the `mathutils.Matrix` surface these handlers touch."""

    def __init__(self, rows) -> None:
        self.rows = [list(row) for row in rows]

    @classmethod
    def at(cls, x: float, y: float, z: float) -> "_Matrix4":
        """Build the matrix that translates to (x, y, z)."""
        return cls([[1.0, 0.0, 0.0, x], [0.0, 1.0, 0.0, y], [0.0, 0.0, 1.0, z], [0.0, 0.0, 0.0, 1.0]])

    @property
    def translation(self) -> tuple[float, float, float]:
        """Read the translation column."""
        return (self.rows[0][3], self.rows[1][3], self.rows[2][3])

    def decompose(self) -> tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]:
        """Split into location, rotation quaternion, and scale, as mathutils does."""
        return self.translation, (1.0, 0.0, 0.0, 0.0), (1.0, 1.0, 1.0)

    def copy(self) -> "_Matrix4":
        """Copy, as mathutils does."""
        return _Matrix4(self.rows)

    def __iter__(self):
        return iter(self.rows)


class _DepsgraphObject:
    """
    An object whose `matrix_world` is owned by the dependency graph, as Blender's is.

    Writing `location` or `matrix_basis` leaves `matrix_world` holding its
    previous value - identity while the object has never been evaluated - until
    `view_layer.update()` re-evaluates it. That lag is the whole defect: a reply
    built by decomposing an unflushed `matrix_world` describes a scene state that
    no longer exists.
    """

    def __init__(self, name: str, parent: "_DepsgraphObject | None" = None) -> None:
        self.name = name
        self.library = None
        self.parent = parent
        self.location = [0.0, 0.0, 0.0]
        self.rotation_mode = "XYZ"
        self.rotation_euler = types.SimpleNamespace(x=0.0, y=0.0, z=0.0)
        self.scale = [1.0, 1.0, 1.0]
        self.matrix_world = _Matrix4.at(0.0, 0.0, 0.0)

    @property
    def matrix_basis(self) -> _Matrix4:
        """Read the object's own channels, which never lag behind a write."""
        return _Matrix4.at(*self.location)

    @matrix_basis.setter
    def matrix_basis(self, matrix: _Matrix4) -> None:
        self.location = list(matrix.translation)

    def evaluate(self) -> None:
        """Re-evaluate this object's world matrix from its parent's, as the graph does."""
        origin = self.parent.matrix_world.translation if self.parent is not None else (0.0, 0.0, 0.0)
        x, y, z = (base + own for base, own in zip(origin, self.location, strict=True))
        self.matrix_world = _Matrix4.at(x, y, z)


def test_set_object_transform_reports_the_world_transform_the_scene_now_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A LOCAL patch's reply reports the evaluated world transform, not the pre-edit one.

    The reported defect: `location` was right while `matrix_world`,
    `world_location` and `world_rotation_quaternion` in the same reply echoed
    the object's world transform from before the edit.
    """
    pivot = _DepsgraphObject("Pivot")
    child = _DepsgraphObject("Child", parent=pivot)
    pivot.location = [10.0, 0.0, 0.0]
    objects = FakeCollection()
    objects["Pivot"] = pivot
    objects["Child"] = child
    addon, bpy = _load_addon(monkeypatch, data={"objects": objects})

    def evaluate_scene() -> None:
        for obj in objects:
            obj.evaluate()

    bpy.context.view_layer = types.SimpleNamespace(update=evaluate_scene)
    monkeypatch.setattr(sys.modules["mathutils"], "Matrix", _Matrix4, raising=False)
    evaluate_scene()
    handler = addon.BlenderMCPServer()

    reply = handler.set_object_transform("Child", {"matrix": _Matrix4.at(1.0, 2.0, 3.0).rows}, "LOCAL")

    assert reply["location"] == [1.0, 2.0, 3.0]
    assert reply["world_location"] == [11.0, 2.0, 3.0]
    assert [row[3] for row in reply["matrix_world"][:3]] == [11.0, 2.0, 3.0]
