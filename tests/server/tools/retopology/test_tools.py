"""Server-layer regression coverage for the retopology command surface."""

import asyncio

import pytest

from test_mutation_transaction import _load_addon

from blender_mcp.server.tools import _dispatch, mesh
from blender_mcp.server.tools.envelope import STALE_INDEX_WARNING
from blender_mcp.server.tools.retopology import advanced, construction, editing, production, quality, target

RETOPOLOGY_TOOL_NAMES = {
    "create_retopology_target",
    "inspect_retopology",
    "analyze_surface_conformity",
    "manage_retopology_checkpoint",
    "configure_surface_projection",
    "project_mesh_elements",
    "build_quad_patch",
    "extend_boundary",
    "mesh_bridge",
    "fill_boundary_quads",
    "reroute_topology",
    "relax_topology",
    "redistribute_edge_loop",
    "configure_retopology_symmetry",
    "validate_retopology",
    "create_retopology_guides",
    "create_surface_section",
    "set_retopology_features",
    "add_support_loops",
    "transfer_mesh_attributes",
    "unwrap_retopology_uvs",
    "create_bake_cage",
    "bake_retopology_maps",
    "test_deformation",
    "generate_quadriflow_draft",
    "fit_surface_primitive",
    "bind_surface_deformation",
    "generate_retopology_lods",
}


class StubConnection:
    def __init__(self, result=None) -> None:
        self.result = result or {"name": "Low", "topology_revision": "revision"}
        self.calls = []

    def send_command(self, command, params):
        self.calls.append((command, params))
        return self.result


def test_all_retopology_tools_are_registered() -> None:
    registered = set(target.mcp._tool_manager._tools)

    assert registered >= RETOPOLOGY_TOOL_NAMES


@pytest.mark.parametrize(
    "tool,command,kwargs",
    [
        (target.inspect_retopology, "inspect_retopology", {"object_name": "Low"}),
        (
            quality.analyze_surface_conformity,
            "analyze_surface_conformity",
            {"object_name": "Low", "source_object_name": "High"},
        ),
        (
            editing.project_mesh_elements,
            "project_mesh_elements",
            {"object_name": "Low", "source_object_name": "High", "vertex_indices": [0]},
        ),
        (
            editing.reroute_topology,
            "reroute_topology",
            {"object_name": "Low", "action": "SPLIT", "edge_indices": [0]},
        ),
        (
            quality.validate_retopology,
            "validate_retopology",
            {"object_name": "Low"},
        ),
    ],
)
def test_retopology_tools_forward_without_context(monkeypatch, tool, command, kwargs) -> None:
    connection = StubConnection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    result = asyncio.run(tool(ctx=None, **kwargs))

    assert result["ok"] is True
    assert connection.calls[0][0] == command
    assert "ctx" not in connection.calls[0][1]


def test_configure_projection_forwards_exact_modifier_controls(monkeypatch) -> None:
    connection = StubConnection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    result = asyncio.run(
        editing.configure_surface_projection(
            ctx=None,
            object_name="Low",
            target_object_name="High",
            wrap_method="PROJECT",
            project_axes=(True, False, False),
            positive_direction=False,
            negative_direction=True,
        )
    )

    _, params = connection.calls[0]
    assert params["project_axes"] == (True, False, False)
    assert params["negative_direction"] is True
    assert result["changed_objects"] == ["Low"]


def test_topology_builders_warn_that_indices_are_stale(monkeypatch) -> None:
    connection = StubConnection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    result = asyncio.run(
        construction.build_quad_patch(
            ctx=None,
            object_name="Low",
            corners=[(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
            u_segments=2,
            v_segments=2,
        )
    )

    assert result["warnings"] == [STALE_INDEX_WARNING]


def test_mesh_bridge_sends_separate_loop_and_revision_inputs(monkeypatch) -> None:
    connection = StubConnection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    result = asyncio.run(
        mesh.mesh_bridge(
            ctx=None,
            object_name="Low",
            loop_a_edge_indices=[0, 1, 2, 3],
            loop_b_edge_indices=[4, 5, 6, 7],
            cuts=2,
            interpolation="SURFACE",
            smoothness=0.5,
            twist_offset=1,
            expected_revision="before",
        )
    )

    command, params = connection.calls[0]
    assert command == "mesh_bridge"
    assert params["loop_a_edge_indices"] == [0, 1, 2, 3]
    assert params["loop_b_edge_indices"] == [4, 5, 6, 7]
    assert params["expected_revision"] == "before"
    assert result["warnings"] == [STALE_INDEX_WARNING]


def test_checkpoint_create_reports_hidden_backup_as_changed_object(monkeypatch) -> None:
    connection = StubConnection({"name": "Low", "backup_object": "Low__checkpoint__before"})
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    result = asyncio.run(
        target.manage_retopology_checkpoint(
            ctx=None,
            action="CREATE",
            object_name="Low",
            checkpoint_name="before",
        )
    )

    assert result["changed_objects"] == ["Low__checkpoint__before"]


@pytest.mark.parametrize(
    "tool,command,kwargs",
    [
        (
            construction.set_retopology_features,
            "set_retopology_features",
            {"object_name": "Low", "edge_indices": [1], "sharp": True},
        ),
        (
            production.transfer_mesh_attributes,
            "transfer_mesh_attributes",
            {"source_object_name": "High", "object_name": "Low", "data_types": ["UVS"]},
        ),
        (
            production.unwrap_retopology_uvs,
            "unwrap_retopology_uvs",
            {"object_name": "Low"},
        ),
        (
            quality.test_deformation,
            "test_deformation",
            {"object_name": "Low", "frames": [1, 10]},
        ),
    ],
)
def test_phase_one_tools_forward_agent_inputs(monkeypatch, tool, command, kwargs) -> None:
    connection = StubConnection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    result = asyncio.run(tool(ctx=None, **kwargs))

    assert result["ok"] is True
    assert len(connection.calls) == 1
    assert connection.calls[0][0] == command
    assert "ctx" not in connection.calls[0][1]


def test_create_guides_reports_actual_collision_safe_names(monkeypatch) -> None:
    connection = StubConnection(
        {"created_guide_objects": ["EyeGuide", "EyeGuide.001"], "guides": [], "coordinate_space": "WORLD"}
    )
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    result = asyncio.run(
        construction.create_retopology_guides(
            ctx=None,
            source_object_name="High",
            guides=[{"name": "EyeGuide", "role": "EYE_LOOP", "source_vertex_indices": [0, 1]}],
        )
    )

    assert result["changed_objects"] == ["EyeGuide", "EyeGuide.001"]


def test_support_loops_warn_that_topology_indices_are_stale(monkeypatch) -> None:
    connection = StubConnection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    result = asyncio.run(
        construction.add_support_loops(
            ctx=None,
            object_name="Low",
            edge_indices=[0, 1],
            width=0.1,
            expected_revision="before",
        )
    )

    assert result["warnings"] == [STALE_INDEX_WARNING]
    assert connection.calls[0][1]["expected_revision"] == "before"


def test_bake_reports_image_as_changed_resource(monkeypatch) -> None:
    connection = StubConnection({"image": "Low_NORMAL", "output_path": "/tmp/normal.exr"})
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    result = asyncio.run(
        production.bake_retopology_maps(
            ctx=None,
            object_name="Low",
            high_poly_object_names=["High"],
            map_type="NORMAL",
            output_path="/tmp/normal.exr",
            confirm=True,
        )
    )

    assert result["changed_resources"] == ["Low_NORMAL"]


@pytest.mark.parametrize(
    "tool,command,kwargs,result",
    [
        (
            advanced.generate_quadriflow_draft,
            "generate_quadriflow_draft",
            {"source_object_name": "High", "target_faces": 1200, "seed": 7},
            {"name": "High_QuadriFlowDraft"},
        ),
        (
            advanced.fit_surface_primitive,
            "fit_surface_primitive",
            {
                "source_object_name": "High",
                "primitive": "CYLINDER",
                "source_vertex_indices": [0, 1, 2, 3, 4, 5],
                "expected_source_revision": "before",
                "axis_hint_world": (0.0, 0.0, 1.0),
            },
            {"name": "High_CylinderFit"},
        ),
        (
            advanced.generate_retopology_lods,
            "generate_retopology_lods",
            {"object_name": "Low", "levels": [{"ratio": 0.5}], "confirm": True},
            {"created_objects": ["Low_LOD1"]},
        ),
    ],
)
def test_phase_two_creation_tools_forward_and_report_created_objects(
    monkeypatch, tool, command, kwargs, result
) -> None:
    connection = StubConnection(result)
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    response = asyncio.run(tool(ctx=None, **kwargs))

    assert connection.calls[0][0] == command
    assert "ctx" not in connection.calls[0][1]
    assert all(connection.calls[0][1][key] == value for key, value in kwargs.items())
    assert response["changed_objects"]


def test_surface_deform_idempotent_unbind_reports_no_change(monkeypatch) -> None:
    connection = StubConnection({"name": "Render", "bound": False, "changed": False})
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    result = asyncio.run(advanced.bind_surface_deformation(ctx=None, object_name="Render", action="UNBIND"))

    assert connection.calls[0][0] == "bind_surface_deformation"
    assert result["changed_objects"] == []


def test_addon_dispatch_advertises_all_phase_two_commands(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()

    commands = server._build_command_handlers()

    assert {
        "generate_quadriflow_draft",
        "fit_surface_primitive",
        "bind_surface_deformation",
        "generate_retopology_lods",
    } <= set(commands)
    assert not {
        name
        for name in (
            "generate_quadriflow_draft",
            "fit_surface_primitive",
            "bind_surface_deformation",
            "generate_retopology_lods",
        )
        if server.command_spec(name).read_only
    }
