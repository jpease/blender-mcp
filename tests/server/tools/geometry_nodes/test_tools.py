"""Regression coverage for the structured Geometry Nodes tool surface."""

import asyncio
import sys
import types

import pytest

from conftest import load_addon
from pydantic import ValidationError

from blender_mcp.server.tools import _dispatch, geometry_nodes

FOUNDATION_COMMANDS = {
    "list_procedural_systems",
    "get_geometry_node_graph",
    "get_geometry_node_type_info",
    "create_geometry_node_group",
    "attach_geometry_nodes_modifier",
    "edit_node_group_interface",
    "patch_geometry_node_graph",
    "set_geometry_nodes_inputs",
    "manage_geometry_nodes_modifier",
    "copy_geometry_node_group",
    "inspect_evaluated_geometry",
    "validate_geometry_node_graph",
}

WORKFLOW_COMMANDS = {
    "create_procedural_scatter",
    "create_curve_generator",
    "create_procedural_array",
    "create_surface_paneling",
    "create_procedural_boolean",
    "create_procedural_deformer",
    "create_volume_generator",
    "manage_named_attributes",
    "manage_procedural_instances",
    "run_geometry_nodes_tool",
    "publish_procedural_asset",
}

ADVANCED_COMMANDS = {
    "create_repeat_zone",
    "create_simulation_zone",
    "manage_geometry_nodes_bake",
    "realize_procedural_output",
    "analyze_procedural_performance",
}


def _run(function, **kwargs):
    return asyncio.run(function(ctx=None, **kwargs))


def test_all_planned_geometry_nodes_commands_are_registered() -> None:
    names = FOUNDATION_COMMANDS | WORKFLOW_COMMANDS | ADVANCED_COMMANDS

    assert all(callable(getattr(geometry_nodes, name)) for name in names)
    assert set(geometry_nodes.mcp._tool_manager._tools) >= names


def test_geometry_nodes_dispatch_and_read_only_contract(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()
    names = FOUNDATION_COMMANDS | WORKFLOW_COMMANDS | ADVANCED_COMMANDS
    read_only = {
        "list_procedural_systems",
        "get_geometry_node_graph",
        "get_geometry_node_type_info",
        "inspect_evaluated_geometry",
        "validate_geometry_node_graph",
        "analyze_procedural_performance",
    }

    assert set(server._build_command_handlers()) >= names
    assert all(server.command_spec(name).read_only for name in read_only)
    assert not {name for name in names - read_only if server.command_spec(name).read_only}


def test_bake_inspection_uses_read_only_dispatch(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()

    result = server._run_handler(
        "manage_geometry_nodes_bake",
        lambda **params: {"action": params["action"]},
        {"action": "INSPECT"},
    )

    assert result == {"action": "INSPECT"}


def test_advanced_geometry_nodes_models_enforce_complexity_and_safety() -> None:
    duplicate_items = [
        geometry_nodes.ZoneStateSpec(name="Geometry", socket_type="GEOMETRY"),
        geometry_nodes.ZoneStateSpec(name="Geometry", socket_type="FLOAT"),
    ]
    with pytest.raises(ValueError, match="unique"):
        _run(
            geometry_nodes.create_repeat_zone,
            node_group_name="Growth",
            state_items=duplicate_items,
        )
    with pytest.raises(ValueError, match="iterations"):
        _run(geometry_nodes.create_repeat_zone, node_group_name="Growth", iterations=257)
    with pytest.raises(ValueError, match="confirm_bake"):
        _run(
            geometry_nodes.manage_geometry_nodes_bake,
            object_name="Growth Mesh",
            modifier_name="GeometryNodes",
            action="BAKE",
            bake_id=1,
        )
    with pytest.raises(ValueError, match="confirm_delete"):
        _run(
            geometry_nodes.manage_geometry_nodes_bake,
            object_name="Growth Mesh",
            modifier_name="GeometryNodes",
            action="DELETE",
            bake_id=1,
        )
    with pytest.raises(ValueError, match="confirm_destructive"):
        _run(
            geometry_nodes.realize_procedural_output,
            object_name="Growth Mesh",
            output_name="Growth Delivery",
            collection_name="Deliveries",
            delivery_mode="APPLIED_MODIFIER_COPY",
            modifier_name="GeometryNodes",
        )


def test_repeat_zone_serializes_state_schema_without_context(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {},
    )

    result = _run(
        geometry_nodes.create_repeat_zone,
        node_group_name="Growth",
        state_items=[
            geometry_nodes.ZoneStateSpec(name="Geometry", socket_type="GEOMETRY"),
            geometry_nodes.ZoneStateSpec(name="Offset", socket_type="VECTOR"),
        ],
        iterations=12,
    )

    assert result["ok"] is True
    assert calls[0][0] == "create_repeat_zone"
    assert calls[0][1]["state_items"][1] == {"name": "Offset", "socket_type": "VECTOR"}
    assert calls[0][1]["iterations"] == 12
    assert result["changed_resources"] == ["Growth"]


def test_scatter_and_volume_extensions_serialize_explicit_output_contracts(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {},
    )

    _run(
        geometry_nodes.create_procedural_scatter,
        object_name="Ground",
        group_name="Guides",
        source_type="OBJECT",
        output_type="HAIR_CURVES",
        density_attribute="density",
        selection_attribute="selection",
        orientation="NORMAL",
        guide_length=2.0,
    )
    _run(
        geometry_nodes.create_volume_generator,
        object_name="Fog Source",
        group_name="Fog",
        density_grid_name="fog_density",
        delivery="OPENVDB",
        output_path="/tmp/fog.vdb",
        confirm_write=True,
    )

    assert calls[0][1]["output_type"] == "HAIR_CURVES"
    assert calls[0][1]["source_name"] is None
    assert calls[1][1]["density_grid_name"] == "fog_density"
    assert calls[1][1]["delivery"] == "OPENVDB"
    assert calls[1][1]["confirm_overwrite"] is False

    with pytest.raises(ValueError, match="output_type=VOLUME"):
        _run(
            geometry_nodes.create_volume_generator,
            object_name="Fog Source",
            group_name="Invalid Fog",
            output_type="MESH",
            delivery="OPENVDB",
            output_path="/tmp/fog.vdb",
            confirm_write=True,
        )


def test_bake_requires_explicit_budgets_before_transport() -> None:
    with pytest.raises(ValueError, match="explicit values"):
        _run(
            geometry_nodes.manage_geometry_nodes_bake,
            object_name="Simulation",
            modifier_name="GeometryNodes",
            action="BAKE",
            bake_id=7,
            frame_start=1,
            frame_end=10,
            bake_target="PACKED",
            confirm_bake=True,
        )


def test_interface_and_graph_models_reject_unsafe_shapes() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        geometry_nodes.InterfaceSocketSpec(
            name="Density",
            direction="INPUT",
            socket_type="NodeSocketFloat",
            arbitrary_rna=True,  # pyright: ignore[reportCallIssue]
        )
    with pytest.raises(ValidationError, match="min_value"):
        geometry_nodes.InterfaceSocketSpec(
            name="Density",
            direction="INPUT",
            socket_type="NodeSocketFloat",
            min_value=2.0,
            max_value=1.0,
        )
    with pytest.raises(ValidationError):
        geometry_nodes.GraphEdit(operation="ADD_NODE", properties={"value": float("inf")})


def test_create_group_serializes_explicit_interface(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {},
    )

    result = _run(
        geometry_nodes.create_geometry_node_group,
        name="Scatter Controls",
        sockets=[
            geometry_nodes.InterfaceSocketSpec(
                name="Density",
                direction="INPUT",
                socket_type="NodeSocketFloat",
                default_value=10.0,
                min_value=0.0,
            )
        ],
    )

    assert result["ok"] is True
    assert calls[0][0] == "create_geometry_node_group"
    assert calls[0][1]["sockets"][0]["socket_type"] == "NodeSocketFloat"
    assert result["changed_resources"] == ["Scatter Controls"]


def test_destructive_geometry_nodes_actions_require_confirmation() -> None:
    with pytest.raises(ValueError, match="confirm_destructive"):
        _run(
            geometry_nodes.manage_geometry_nodes_modifier,
            object_name="Cube",
            modifier_name="GeometryNodes",
            action="APPLY",
        )
    with pytest.raises(ValueError, match="confirm_destructive"):
        _run(
            geometry_nodes.run_geometry_nodes_tool,
            node_group_name="Cleanup",
            object_names=["Cube"],
        )
    with pytest.raises(ValueError, match="confirm_destructive"):
        _run(
            geometry_nodes.manage_named_attributes,
            object_name="Cube",
            action="REMOVE",
            attribute_name="mask",
        )


def test_attach_requires_exactly_one_group_source() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        _run(geometry_nodes.attach_geometry_nodes_modifier, object_name="Cube")
    with pytest.raises(ValueError, match="exactly one"):
        _run(
            geometry_nodes.attach_geometry_nodes_modifier,
            object_name="Cube",
            node_group_name="Existing",
            new_group_name="New",
        )


def test_workflow_request_does_not_leak_mcp_context(monkeypatch) -> None:
    workflows = sys.modules["blender_mcp.server.tools.geometry_nodes.workflows"]
    calls = []

    async def fake_build(command, params, object_name, group_name):
        await asyncio.sleep(0)
        calls.append((command, params, object_name, group_name))
        return {"ok": True}

    monkeypatch.setattr(workflows, "_build", fake_build)
    _run(
        geometry_nodes.create_procedural_array,
        object_name="Layout",
        group_name="Radial Layout",
        source_name="Chair",
        layout="RADIAL",
        count=8,
        endpoint_policy="EXCLUDE_END",
    )

    assert calls[0][0] == "create_procedural_array"
    assert "ctx" not in calls[0][1]
    assert calls[0][1]["endpoint_policy"] == "EXCLUDE_END"


def test_copy_group_serializes_object_duplication_policy(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {},
    )

    result = _run(
        geometry_nodes.copy_geometry_node_group,
        node_group_name="Shared Scatter",
        new_name="Independent Scatter",
        duplicate_object_name="Forest",
        duplicated_object_name="Forest Variant",
        copy_object_data=True,
        copy_action=True,
        reassign_duplicate_modifiers=False,
        collision_policy="UNIQUE",
    )

    assert result["ok"] is True
    assert calls[0][0] == "copy_geometry_node_group"
    assert calls[0][1] == {
        "node_group_name": "Shared Scatter",
        "new_name": "Independent Scatter",
        "reassign_modifiers": [],
        "duplicate_object_name": "Forest",
        "duplicated_object_name": "Forest Variant",
        "copy_object_data": True,
        "copy_action": True,
        "reassign_duplicate_modifiers": False,
        "collision_policy": "UNIQUE",
    }
    assert result["changed_objects"] == ["Forest Variant"]


class _Collection(list):
    """A `bpy.data` collection: iterated as datablocks, looked up by each one's current name."""

    def get(self, name: str, default: object = None) -> object:
        return next((held for held in self if held.name == name), default)

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, str):
            found = self.get(key)
            if found is None:
                raise KeyError(key)
            return found
        return super().__getitem__(key)

    def remove(self, datablock, do_unlink=False) -> None:  # type: ignore[override]
        del self[next(index for index, held in enumerate(self) if held is datablock)]


class _NodeGroup:
    """The slice of a GeometryNodeTree an atomic graph patch reads: its name, copy, and graph."""

    bl_idname = "GeometryNodeTree"
    library = None
    is_editable = True

    def __init__(self, name: str, node_groups: _Collection) -> None:
        self.name = name
        self.nodes: list[object] = []
        self.links: list[object] = []
        self._node_groups = node_groups

    def copy(self) -> "_NodeGroup":
        working = _NodeGroup(f"{self.name}.001", self._node_groups)
        self._node_groups.append(working)
        return working


def test_patching_a_widely_used_group_counts_its_user_objects_and_changes_only_the_group(monkeypatch) -> None:
    """A dozen objects running the group are counted and sampled; the group is the only named change."""
    node_groups = _Collection()
    group = _NodeGroup("Scatter", node_groups)
    node_groups.append(group)

    def user(name: str, object_type: str, modifier_count: int = 1) -> types.SimpleNamespace:
        modifiers = [
            types.SimpleNamespace(name=f"GN {index}", type="NODES", node_group=group) for index in range(modifier_count)
        ]
        return types.SimpleNamespace(name=name, type=object_type, modifiers=modifiers)

    users = [user(f"Rock_{index:02d}", "MESH", modifier_count=2 if index == 0 else 1) for index in range(11)]
    users.append(user("Grass", "CURVES"))
    bystander = types.SimpleNamespace(name="Ground", type="MESH", modifiers=[])
    objects = _Collection([*reversed(users), bystander])
    addon, _bpy = load_addon(monkeypatch, data={"objects": objects, "node_groups": node_groups})

    result = addon.BlenderMCPServer().patch_geometry_node_graph("Scatter", [])

    replacement = node_groups["Scatter"]
    assert replacement is not group
    assert all(modifier.node_group is replacement for obj in users for modifier in obj.modifiers)
    assert result["affected_users"] == {
        "total": 12,
        "by_type": {"CURVES": 1, "MESH": 11},
        "limit": 10,
        "returned_count": 10,
        "truncated": True,
        "names": ["Grass", *(f"Rock_{index:02d}" for index in range(9))],
    }
    monkeypatch.setattr(_dispatch, "send_command", lambda _command, _params=None: result)
    envelope = _run(
        geometry_nodes.patch_geometry_node_graph,
        node_group_name="Scatter",
        operations=[geometry_nodes.GraphEdit(operation="ADD_NODE", bl_idname="GeometryNodeJoinGeometry")],
    )
    assert envelope["changed_objects"] == []
    assert envelope["changed_resources"] == ["Scatter"]
