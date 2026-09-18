"""Run with Blender 5.1+ to smoke-test production PBR texturing operations."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile

from pathlib import Path

import bpy

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon" / "__init__.py"
package_name = "blender_mcp_texture_smoke"
spec = importlib.util.spec_from_file_location(
    package_name, addon_path, submodule_search_locations=[str(addon_path.parent)]
)
assert spec is not None
addon = importlib.util.module_from_spec(spec)
sys.modules[package_name] = addon
spec.loader.exec_module(addon)

from blender_mcp_texture_smoke.handlers.texture import TextureHandlers  # noqa: E402


def main() -> None:
    """Exercise material, image, assignment, mapping, UV, validation, and preview handlers."""
    handler = TextureHandlers()
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    obj.name = "PBR Smoke Cube"
    uv = obj.data.uv_layers.new(name="UVMap")
    obj.data.uv_layers.active = uv

    created = handler.create_pbr_material(
        "PBR Smoke Material",
        "BOTH",
        {"base_color": (0.35, 0.1, 0.04, 1.0), "roughness": 0.42},
    )
    assert created["created"]
    configured = handler.configure_pbr_material(
        "PBR Smoke Material", {"metallic": 0.2, "surface_render_method": "DITHERED"}, "BOTH"
    )
    assert abs(configured["after"]["metallic"] - 0.2) < 1e-6
    assigned = handler.assign_material("PBR Smoke Material", [obj.name])
    assert assigned["assignments"][0]["slot_index"] == 0

    with tempfile.TemporaryDirectory(prefix="blender_mcp_texture_") as directory:
        source_path = os.path.join(directory, "base_color.png")
        source = bpy.data.images.new("PBR Smoke Source", width=8, height=8, alpha=True)
        source.generated_color = (0.6, 0.2, 0.1, 1.0)
        source.filepath_raw = source_path
        source.file_format = "PNG"
        source.save()
        bpy.data.images.remove(source)

        loaded = handler.load_texture_image(source_path, "PBR Smoke Base Color")
        assert loaded["loaded"]
        interpreted = handler.configure_texture_image("PBR Smoke Base Color", semantic="COLOR")
        assert interpreted["after"]["colorspace"] == "sRGB"
        saved_path = os.path.join(directory, "saved_copy.png")
        saved = handler.save_texture_image("PBR Smoke Base Color", saved_path, "PNG", "RGBA", "8")
        assert saved["file_size_bytes"] > 0
        # `~` must mean the home directory, not a directory named `~` under Blender's working directory.
        previous_home = os.environ.get("HOME")
        os.environ["HOME"] = directory
        try:
            handler.save_texture_image("PBR Smoke Base Color", "~/tilde_copy.png", "PNG", "RGBA", "8")
            assert os.path.isfile(os.path.join(directory, "tilde_copy.png"))
        finally:
            if previous_home is None:
                del os.environ["HOME"]
            else:
                os.environ["HOME"] = previous_home
        texture_set = handler.apply_pbr_texture_set(
            "PBR Smoke Material", {"base_color": source_path}, uv_map_name="UVMap"
        )
        texture_node = texture_set["nodes"]["base_color"]
        mapping = handler.configure_texture_mapping(
            "PBR Smoke Material",
            [texture_node],
            {"coordinate_source": "UV", "uv_map_name": "UVMap", "scale": (2.0, 2.0, 2.0)},
        )
        assert mapping["mapping_node"]

        preview_path = os.path.join(directory, "preview.png")
        preview = handler.render_pbr_material_preview(
            "PBR Smoke Material",
            "BLENDER_EEVEE_NEXT",
            "SPHERE",
            64,
            1,
            False,
            {"BLENDER_EEVEE_NEXT": preview_path},
        )
        assert preview["outputs"][0]["size_bytes"] > 0

    seams = handler.set_uv_seams(obj.name, "MARK", edge_indices=[0, 1])
    assert seams["changed_edge_indices"] == [0, 1]
    unwrapped = handler.unwrap_uvs(obj.name, "UVMap", "ANGLE_BASED")
    assert unwrapped["uv_map"] == "UVMap"
    optimized = handler.optimize_uv_layout(obj.name, "UVMap", minimize_stretch_iterations=1)
    assert "PACK_ISLANDS" in optimized["stages"]
    inspected = handler.inspect_uv_layout(obj.name, "UVMap", 10)
    assert inspected["uv_maps"][0]["uv_map"] == "UVMap"
    uv_maps = handler.manage_uv_maps(obj.name, "LIST")
    assert uv_maps["uv_maps"][0]["name"] == "UVMap"
    inventory = handler.list_materials(object_name=obj.name)
    assert inventory["materials"][0]["name"] == "PBR Smoke Material"
    detailed = handler.inspect_material("PBR Smoke Material")
    assert detailed["active_output"] == "PBR Material Output"
    node_type = handler.get_shader_node_type_info("MATERIAL", bl_idname="ShaderNodeTexNoise")
    assert node_type["available"] and node_type["creatable"]
    assert any(socket["identifier"] == "Scale" for socket in node_type["inputs"])
    patched = handler.patch_shader_graph(
        {"type": "MATERIAL", "name": "PBR Smoke Material"},
        [
            {
                "operation": "ADD_NODE",
                "bl_idname": "ShaderNodeTexNoise",
                "new_name": "PBR Smoke Noise",
                "managed_role": "smoke_noise",
            },
            {
                "operation": "SET_INPUT",
                "node_name": "PBR Smoke Noise",
                "socket_identifier": "Scale",
                "value": 3.25,
            },
        ],
    )
    assert patched["node_count"] == len(bpy.data.materials["PBR Smoke Material"].node_tree.nodes)
    assert bpy.data.materials["PBR Smoke Material"].node_tree.nodes["PBR Smoke Noise"]["mcp_owner"] == "blender-mcp"
    assert obj.material_slots[0].material == bpy.data.materials["PBR Smoke Material"]
    committed_material = bpy.data.materials["PBR Smoke Material"]
    try:
        handler.patch_shader_graph(
            {"type": "MATERIAL", "name": committed_material.name},
            [
                {
                    "operation": "ADD_NODE",
                    "bl_idname": "ShaderNodeTexVoronoi",
                    "new_name": "Must Roll Back",
                },
                {"operation": "REMOVE_NODE", "node_name": "PBR Material Output"},
            ],
        )
    except ValueError as exc:
        assert "must contain ShaderNodeOutputMaterial" in str(exc)
    else:
        raise AssertionError("An incomplete material graph must be rejected")
    assert bpy.data.materials["PBR Smoke Material"] is committed_material
    assert committed_material.node_tree.nodes.get("Must Roll Back") is None

    world = bpy.data.worlds.new("PBR Smoke World")
    bpy.context.scene.world = world
    patched_world = handler.patch_shader_graph(
        {"type": "WORLD", "name": world.name},
        [
            {
                "operation": "ADD_NODE",
                "bl_idname": "ShaderNodeTexSky",
                "new_name": "PBR Smoke Sky",
            }
        ],
        enable_nodes=True,
    )
    assert patched_world["target"]["name"] == "PBR Smoke World"
    assert bpy.context.scene.world.node_tree.nodes.get("PBR Smoke Sky") is not None

    light_data = bpy.data.lights.new("PBR Smoke Light", "POINT")
    light_object = bpy.data.objects.new("PBR Smoke Light", light_data)
    bpy.context.scene.collection.objects.link(light_object)
    patched_light = handler.patch_shader_graph(
        {"type": "LIGHT", "name": light_data.name},
        [
            {
                "operation": "ADD_NODE",
                "bl_idname": "ShaderNodeTexNoise",
                "new_name": "PBR Smoke Light Noise",
            }
        ],
        enable_nodes=True,
    )
    assert patched_light["users"] == [light_object.name]
    assert light_object.data.node_tree.nodes.get("PBR Smoke Light Noise") is not None
    validation = handler.validate_pbr_asset([obj.name], profile="BLENDER_BOTH")
    assert "findings" in validation

    water = handler.create_pbr_material("PBR Smoke Water", "BOTH", preset="WATER")
    assert water["created"]
    assert water["preset"] == "WATER"
    assert water["volume_node"]
    material = bpy.data.materials["PBR Smoke Water"]
    volume_input = next(
        node for node in material.node_tree.nodes if node.bl_idname == "ShaderNodeOutputMaterial"
    ).inputs["Volume"]
    assert volume_input.is_linked

    removed = handler.configure_pbr_material("PBR Smoke Water", {"volume_density": 0.0}, "BOTH")
    assert removed["volume_node"] is None
    assert not volume_input.is_linked

    print("TEXTURE_SMOKE_OK")


if __name__ == "__main__":
    main()
