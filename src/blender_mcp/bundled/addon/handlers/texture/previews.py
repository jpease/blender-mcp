# pyright: reportAttributeAccessIssue=false
"""Deterministic isolated PBR material preview rendering."""

import os

import bmesh
import bpy
import mathutils

from ._shared import material_by_name, runtime_engine, validate_engine


def _point_camera(camera, target):
    camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()


def _preview_mesh(name, geometry):
    mesh = bpy.data.meshes.new(f"{name} Mesh")
    bm = bmesh.new()
    if geometry == "SPHERE":
        bmesh.ops.create_uvsphere(bm, u_segments=64, v_segments=32, radius=1.0)
    elif geometry == "PLANE":
        bmesh.ops.create_grid(bm, x_segments=16, y_segments=16, size=2.0)
    elif geometry == "ROUNDED_CUBE":
        bmesh.ops.create_cube(bm, size=2.0)
        bevel = bmesh.ops.bevel(
            bm,
            geom=list(bm.edges),
            offset=0.16,
            segments=4,
            affect="EDGES",
        )
        del bevel
    else:
        bm.free()
        bpy.data.meshes.remove(mesh)
        raise ValueError("geometry must be SPHERE, PLANE, or ROUNDED_CUBE")
    bm.to_mesh(mesh)
    bm.free()
    return mesh


class TexturePreviewHandlers:
    """Render materials in a disposable, reproducible studio scene."""

    def render_pbr_material_preview(
        self,
        material_name,
        target_engine="BLENDER_EEVEE_NEXT",
        geometry="SPHERE",
        resolution=512,
        samples=64,
        transparent_background=False,
        output_paths=None,
    ):
        material = material_by_name(material_name)
        target_engine = validate_engine(target_engine)
        engines = [target_engine] if target_engine != "BOTH" else ["CYCLES", "BLENDER_EEVEE_NEXT"]
        paths = dict(output_paths or {})
        if set(paths) != set(engines):
            raise ValueError(f"output_paths must contain exactly {engines}")
        for engine, path in paths.items():
            if not os.path.isabs(path) or os.path.splitext(path)[1].lower() != ".png":
                raise ValueError(f"{engine} output path must be an absolute .png path")
            if not os.path.isdir(os.path.dirname(path)):
                raise ValueError(f"Output directory does not exist: {os.path.dirname(path)}")
            if os.path.exists(path):
                raise ValueError(f"Preview output already exists: {path}")
        scene = bpy.data.scenes.new("MCP PBR Preview")
        collection = bpy.data.collections.new("MCP PBR Preview Assets")
        scene.collection.children.link(collection)
        created_objects, created_data = [], []
        outputs = []
        try:
            mesh = _preview_mesh("MCP Preview Surface", geometry)
            created_data.append(mesh)
            subject = bpy.data.objects.new("MCP Preview Surface", mesh)
            created_objects.append(subject)
            collection.objects.link(subject)
            subject.data.materials.append(material)
            camera_data = bpy.data.cameras.new("MCP Preview Camera")
            created_data.append(camera_data)
            camera = bpy.data.objects.new("MCP Preview Camera", camera_data)
            created_objects.append(camera)
            collection.objects.link(camera)
            camera.location = (3.2, -3.2, 2.4)
            _point_camera(camera, mathutils.Vector((0, 0, 0)))
            camera_data.lens = 55
            scene.camera = camera
            for name, location, energy, size in (
                ("Key", (3.0, -2.0, 4.0), 900.0, 3.0),
                ("Fill", (-3.0, -1.0, 2.0), 450.0, 4.0),
                ("Rim", (1.0, 3.0, 3.0), 700.0, 2.0),
            ):
                light_data = bpy.data.lights.new(f"MCP Preview {name}", "AREA")
                light_data.energy, light_data.shape, light_data.size = energy, "DISK", size
                created_data.append(light_data)
                light = bpy.data.objects.new(f"MCP Preview {name}", light_data)
                created_objects.append(light)
                collection.objects.link(light)
                light.location = location
                _point_camera(light, mathutils.Vector((0, 0, 0)))
            world = bpy.data.worlds.new("MCP PBR Preview World")
            created_data.append(world)
            world.use_nodes = True
            background = world.node_tree.nodes.get("Background")
            background.inputs["Color"].default_value = (0.05, 0.05, 0.05, 1.0)
            background.inputs["Strength"].default_value = 0.25
            scene.world = world
            render = scene.render
            render.resolution_x = render.resolution_y = int(resolution)
            render.resolution_percentage = 100
            render.image_settings.file_format = "PNG"
            render.image_settings.color_mode = "RGBA"
            render.film_transparent = bool(transparent_background)
            view_items = {
                item.identifier for item in scene.view_settings.bl_rna.properties["view_transform"].enum_items
            }
            if "AgX" in view_items:
                scene.view_settings.view_transform = "AgX"
            look_items = {item.identifier for item in scene.view_settings.bl_rna.properties["look"].enum_items}
            for look in ("AgX - Medium High Contrast", "Medium High Contrast", "None"):
                if look in look_items:
                    scene.view_settings.look = look
                    break
            color_management = {
                "view_transform": scene.view_settings.view_transform,
                "look": scene.view_settings.look,
                "exposure": float(scene.view_settings.exposure),
            }
            for engine in engines:
                scene.render.engine = runtime_engine(engine)
                if engine == "CYCLES":
                    scene.cycles.samples = int(samples)
                # Blender 5.1 exposes Eevee samples through render settings RNA;
                # retain runtime introspection for builds with renamed properties.
                elif hasattr(scene, "eevee") and hasattr(scene.eevee, "taa_render_samples"):
                    scene.eevee.taa_render_samples = int(samples)
                render.filepath = paths[engine]
                with bpy.context.temp_override(scene=scene):
                    result = bpy.ops.render.render(write_still=True, scene=scene.name)
                if "FINISHED" not in result or not os.path.isfile(paths[engine]):
                    raise RuntimeError(f"{engine} preview did not create its PNG")
                outputs.append(
                    {
                        "engine": engine,
                        "path": paths[engine],
                        "size_bytes": os.path.getsize(paths[engine]),
                        "samples": int(samples),
                    }
                )
        finally:
            for obj in created_objects:
                if bpy.data.objects.get(obj.name):
                    bpy.data.objects.remove(obj, do_unlink=True)
            bpy.data.scenes.remove(scene)
            for datablock in created_data:
                collection_name = type(datablock).__name__
                del collection_name
                if getattr(datablock, "users", 0) == 0:
                    for store in (bpy.data.meshes, bpy.data.cameras, bpy.data.lights, bpy.data.worlds):
                        if store.get(datablock.name) == datablock:
                            store.remove(datablock)
                            break
            if bpy.data.collections.get(collection.name) and collection.users == 0:
                bpy.data.collections.remove(collection)
        return {
            "material": material.name,
            "geometry": geometry,
            "resolution": int(resolution),
            "outputs": outputs,
            "color_management": color_management,
            "warnings": ["True displacement is not represented in Eevee previews."]
            if target_engine in {"BOTH", "EEVEE", "BLENDER_EEVEE_NEXT"}
            and getattr(material, "displacement_method", "BUMP") != "BUMP"
            else [],
            "changed_objects": [],
            "changed_resources": [],
        }
