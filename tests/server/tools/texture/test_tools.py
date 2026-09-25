"""Public schema, package organization, transport, and dispatch coverage for PBR texturing."""

import asyncio
import inspect

from pathlib import Path

import pytest

from conftest import load_addon
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from blender_mcp.server.tools import _dispatch, texture

TEXTURE_COMMANDS = {
    "list_materials",
    "inspect_material",
    "get_shader_node_type_info",
    "patch_shader_graph",
    "create_pbr_material",
    "configure_pbr_material",
    "assign_material",
    "configure_texture_mapping",
    "list_texture_images",
    "load_texture_image",
    "configure_texture_image",
    "apply_pbr_texture_set",
    "save_texture_image",
    "render_pbr_material_preview",
    "manage_uv_maps",
    "set_uv_seams",
    "unwrap_uvs",
    "optimize_uv_layout",
    "inspect_uv_layout",
    "bake_texture_map",
    "validate_pbr_asset",
}
READ_ONLY_TEXTURE_COMMANDS = {
    "list_materials",
    "inspect_material",
    "get_shader_node_type_info",
    "list_texture_images",
    "inspect_uv_layout",
    "validate_pbr_asset",
}


class StubConnection:
    """Record commands sent through a texturing tool."""

    def __init__(self, result=None):
        self.result = result or {"status": "ok"}
        self.calls = []

    def send_command(self, command, params):
        self.calls.append((command, params))
        return self.result


def run_tool(function, **kwargs):
    return asyncio.run(function(ctx=None, **kwargs))


def test_all_texture_commands_are_public_and_grouped_by_responsibility():
    assert all(callable(getattr(texture, name)) for name in TEXTURE_COMMANDS)
    expected_modules = {"materials.py", "images.py", "uv.py", "baking.py", "previews.py", "validation.py"}
    assert expected_modules.issubset({path.name for path in Path(texture.__file__).parent.iterdir()})


def test_material_patch_is_strict_and_rejects_nonfinite_values():
    with pytest.raises(ValidationError):
        texture.PBRMaterialSettings(arbitrary_rna=True)  # pyright: ignore[reportCallIssue]
    with pytest.raises(ValidationError):
        texture.PBRMaterialSettings(roughness=float("nan"))
    with pytest.raises(ValidationError):
        texture.PBRMaterialSettings(base_color=(1.2, 0.2, 0.2, 1.0))


def test_material_patch_requires_volume_fields_together():
    with pytest.raises(ValidationError, match="volume_absorption_color and volume_density"):
        texture.PBRMaterialSettings(volume_density=0.1)
    with pytest.raises(ValidationError, match="volume_absorption_color and volume_density"):
        texture.PBRMaterialSettings(volume_absorption_color=(1.0, 1.0, 1.0, 1.0))
    with pytest.raises(ValidationError):
        texture.PBRMaterialSettings(volume_absorption_color=(1.2, 1.0, 1.0, 1.0), volume_density=0.1)
    settings = texture.PBRMaterialSettings(volume_absorption_color=(0.5, 0.5, 0.5, 1.0), volume_density=0.1)
    assert settings.volume_density == pytest.approx(0.1)


def test_create_material_forwards_preset_and_volume_settings(monkeypatch):
    connection = StubConnection({"material": "Water", "created": True, "changed_resources": ["Water"]})
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    run_tool(
        texture.create_pbr_material,
        material_name="Water",
        preset="WATER",
        settings=texture.PBRMaterialSettings(volume_density=0.05, volume_absorption_color=(0.7, 0.9, 1.0, 1.0)),
    )

    assert connection.calls == [
        (
            "create_pbr_material",
            {
                "material_name": "Water",
                "target_engine": "BOTH",
                "preset": "WATER",
                "settings": {"volume_density": 0.05, "volume_absorption_color": (0.7, 0.9, 1.0, 1.0)},
                "reuse_existing": False,
            },
        )
    ]


def test_configure_material_sends_only_explicit_fields(monkeypatch):
    connection = StubConnection({"material": "Paint", "changed_resources": ["Paint"]})
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    result = run_tool(
        texture.configure_pbr_material,
        material_name="Paint",
        patch=texture.PBRMaterialSettings(roughness=0.25, metallic=0.8),
    )

    assert connection.calls == [
        (
            "configure_pbr_material",
            {"material_name": "Paint", "patch": {"metallic": 0.8, "roughness": 0.25}, "target_engine": "BOTH"},
        )
    ]
    assert result["changed_resources"] == ["Paint"]


def test_texture_set_rejects_ambiguous_semantic_channels():
    with pytest.raises(ValidationError, match="roughness or glossiness"):
        texture.TextureSetFiles(roughness="/tmp/r.png", glossiness="/tmp/g.png")
    with pytest.raises(ValidationError, match="normal_opengl or normal_directx"):
        texture.TextureSetFiles(normal_opengl="/tmp/n.png", normal_directx="/tmp/n_dx.png")


def test_shader_graph_patch_is_strict_and_serializes_stable_socket_identity(monkeypatch):
    connection = StubConnection({"target": {"type": "MATERIAL", "name": "Paint"}})
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    with pytest.raises(ValidationError):
        texture.ShaderGraphEdit(operation="ADD_NODE", properties={"value": float("inf")})

    result = run_tool(
        texture.patch_shader_graph,
        target=texture.ShaderGraphTarget(type="MATERIAL", name="Paint"),
        operations=[
            texture.ShaderGraphEdit(
                operation="ADD_NODE",
                bl_idname="ShaderNodeTexNoise",
                new_name="Procedural Noise",
                managed_role="surface_noise",
            ),
            texture.ShaderGraphEdit(
                operation="SET_INPUT",
                node_name="Procedural Noise",
                socket_identifier="Scale",
                socket_index=2,
                value=4.5,
            ),
        ],
    )

    assert result["ok"]
    assert connection.calls == [
        (
            "patch_shader_graph",
            {
                "target": {"type": "MATERIAL", "name": "Paint"},
                "operations": [
                    {
                        "operation": "ADD_NODE",
                        "bl_idname": "ShaderNodeTexNoise",
                        "properties": {},
                        "new_name": "Procedural Noise",
                        "managed_role": "surface_noise",
                    },
                    {
                        "operation": "SET_INPUT",
                        "node_name": "Procedural Noise",
                        "properties": {},
                        "socket_identifier": "Scale",
                        "socket_index": 2,
                        "value": 4.5,
                    },
                ],
                "enable_nodes": False,
            },
        )
    ]


def test_destructive_or_expensive_inputs_are_gated_before_dispatch(monkeypatch):
    connection = StubConnection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    with pytest.raises(ToolError, match="confirm_bake"):
        run_tool(texture.bake_texture_map, object_name="Low", map_type="NORMAL", output_path="/tmp/n.png")
    with pytest.raises(ToolError, match="confirm_cycles"):
        run_tool(texture.render_pbr_material_preview, material_name="Paint", target_engine="CYCLES")

    assert connection.calls == []


def test_texture_tools_are_async_and_dispatch_is_complete(monkeypatch):
    assert all(inspect.iscoroutinefunction(getattr(texture, name)) for name in TEXTURE_COMMANDS)
    addon, _bpy = load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()

    commands = server._build_command_handlers()

    assert TEXTURE_COMMANDS.issubset(commands)
    assert all(server.command_spec(name).read_only for name in READ_ONLY_TEXTURE_COMMANDS)
    assert not {name for name in TEXTURE_COMMANDS - READ_ONLY_TEXTURE_COMMANDS if server.command_spec(name).read_only}
