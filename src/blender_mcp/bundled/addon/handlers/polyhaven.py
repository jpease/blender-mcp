import os
import shutil
import tempfile

from contextlib import suppress

import bpy

from ..constants import REQ_HEADERS
from ..file_paths import enforce_roots, resolve_blend_path, sanitize_blender_error
from ..network import download_file, get_json
from .file_lifecycle import _refuse_scripts_auto_execute

_MAX_IMAGE_BYTES = 512 * 1024 * 1024
_MAX_MODEL_FILE_BYTES = 2 * 1024 * 1024 * 1024


def _validated_download(path: str, download_dir: str) -> str:
    """
    Check a downloaded `.blend` before Blender parses it.

    The download is untrusted. The boundary is the handler's own temp directory,
    not the deployment's file roots, because no caller named this path.

    Args:
        path: Where the download was written.
        download_dir: The temp directory the handler created for it.

    Returns:
        str: The canonical path to load.

    Raises:
        ValueError: If the file is not a `.blend` or resolves outside the directory.

    """
    blend_path = resolve_blend_path(path, must_exist=True)
    try:
        enforce_roots(blend_path, [download_dir])
    except ValueError:
        raise ValueError("downloaded file resolves outside its download directory") from None
    return blend_path


class PolyhavenHandlersMixin:
    """Provide handlers for browsing and importing Poly Haven assets."""

    def get_polyhaven_categories(self, asset_type):
        """
        Get categories for a specific asset type from Polyhaven.

        Args:
            asset_type: Value for asset type.

        Returns:
            Result produced by the operation.

        """
        try:
            if asset_type not in {"hdris", "textures", "models", "all"}:
                return {"error": f"Invalid asset type: {asset_type}. Must be one of: hdris, textures, models, all"}

            return {
                "categories": get_json(
                    f"https://api.polyhaven.com/categories/{asset_type}",
                    headers=REQ_HEADERS,
                )
            }
        except Exception as e:
            return {"error": sanitize_blender_error(e)}

    def list_polyhaven_assets(self, asset_type=None, categories=None, limit=20, offset=0):
        """
        Search for assets from Polyhaven with optional filtering.

        Args:
            asset_type: Value for asset type.
            categories: Value for categories.

        Returns:
            Result produced by the operation.

        """
        try:
            url = "https://api.polyhaven.com/assets"
            params = {}

            if asset_type and asset_type != "all":
                if asset_type not in {"hdris", "textures", "models"}:
                    return {"error": f"Invalid asset type: {asset_type}. Must be one of: hdris, textures, models, all"}
                params["type"] = asset_type

            if categories:
                params["categories"] = categories

            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
                return {"error": "limit must be an integer from 1 through 100"}
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                return {"error": "offset must be a non-negative integer"}
            assets = get_json(url, params=params, headers=REQ_HEADERS)
            if not isinstance(assets, dict):
                return {"error": "Poly Haven returned an unexpected catalog response"}
            ordered = sorted(assets.items(), key=lambda item: item[0].casefold())
            page = ordered[offset : offset + limit]
            limited_assets = dict(page)
            next_offset = offset + len(page)
            truncated = next_offset < len(ordered)

            return {
                "assets": limited_assets,
                "total_count": len(assets),
                "returned_count": len(limited_assets),
                "offset": min(offset, len(ordered)),
                "limit": limit,
                "truncated": truncated,
                "next_offset": next_offset if truncated else None,
            }
        except Exception as e:
            return {"error": sanitize_blender_error(e)}

    def import_polyhaven_asset(self, asset_id, asset_type, resolution="1k", file_format=None):
        try:
            # First get the files information
            files_data = get_json(f"https://api.polyhaven.com/files/{asset_id}", headers=REQ_HEADERS)

            # Handle different asset types
            if asset_type == "hdris":
                # For HDRIs, download the .hdr or .exr file
                if not file_format:
                    file_format = "hdr"  # Default format for HDRIs
                file_format = file_format.lower()
                if file_format not in {"hdr", "exr"}:
                    return {"error": "Poly Haven HDRIs require file_format 'hdr' or 'exr'"}
                if (
                    not isinstance(resolution, str)
                    or not resolution
                    or any(
                        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                        for character in resolution
                    )
                ):
                    return {"error": "resolution contains unsupported filename characters"}

                if (
                    "hdri" in files_data
                    and resolution in files_data["hdri"]
                    and file_format in files_data["hdri"][resolution]
                ):
                    file_info = files_data["hdri"][resolution][file_format]
                    file_url = file_info["url"]
                    safe_asset_id = "".join(
                        character if character.isalnum() or character in {"-", "_"} else "_" for character in asset_id
                    ).strip("_")
                    if not safe_asset_id:
                        return {"error": "asset_id does not contain a safe filename component"}
                    cache_directory = bpy.utils.user_resource(
                        "DATAFILES",
                        path=os.path.join("blender_mcp", "polyhaven"),
                        create=True,
                    )
                    if not cache_directory:
                        return {"error": "Could not create the Blender MCP Poly Haven cache directory"}
                    persistent_path = os.path.join(
                        cache_directory,
                        f"{safe_asset_id}_{resolution}.{file_format}",
                    )
                    partial_path = f"{persistent_path}.part"
                    try:
                        download_file(file_url, partial_path, headers=REQ_HEADERS, max_bytes=_MAX_IMAGE_BYTES)
                        os.replace(partial_path, persistent_path)
                        configured = self.configure_hdri_environment(
                            scene_name=bpy.context.scene.name,
                            image_path=persistent_path,
                            strength=1.0,
                            rotation=0.0,
                            projection="EQUIRECTANGULAR",
                            replacement_policy="REPLACE_MANAGED",
                            world_name="World" if bpy.context.scene.world is None else None,
                            create_world=bpy.context.scene.world is None,
                        )
                        return {
                            "success": True,
                            "message": f"HDRI {asset_id} imported successfully",
                            "image_name": configured["image"],
                            "image_path": configured["image_path"],
                            "world": configured["world"],
                        }
                    except Exception as e:
                        return {"error": f"Failed to set up HDRI in Blender: {sanitize_blender_error(e)}"}
                    finally:
                        with suppress(FileNotFoundError):
                            os.remove(partial_path)
                else:
                    return {"error": "Requested resolution or format not available for this HDRI"}

            elif asset_type == "textures":
                if not file_format:
                    file_format = "jpg"  # Default format for textures

                downloaded_maps = {}

                try:
                    for map_type in files_data:
                        if map_type not in {"blend", "gltf"}:  # Skip non-texture files
                            if resolution in files_data[map_type] and file_format in files_data[map_type][resolution]:
                                file_info = files_data[map_type][resolution][file_format]
                                file_url = file_info["url"]

                                # Use NamedTemporaryFile like we do for HDRIs
                                with tempfile.NamedTemporaryFile(suffix=f".{file_format}", delete=False) as tmp_file:
                                    tmp_path = tmp_file.name
                                try:
                                    download_file(file_url, tmp_path, headers=REQ_HEADERS, max_bytes=_MAX_IMAGE_BYTES)
                                    image = bpy.data.images.load(tmp_path)
                                    image.name = f"{asset_id}_{map_type}.{file_format}"
                                    image.pack()
                                    if map_type in {"color", "diffuse", "albedo"}:
                                        with suppress(Exception):
                                            image.colorspace_settings.name = "sRGB"
                                    else:
                                        with suppress(Exception):
                                            image.colorspace_settings.name = "Non-Color"
                                    downloaded_maps[map_type] = image
                                finally:
                                    with suppress(FileNotFoundError):
                                        os.unlink(tmp_path)

                    if not downloaded_maps:
                        return {"error": "No texture maps found for the requested resolution and format"}

                    # Create a new material with the downloaded textures
                    mat = bpy.data.materials.new(name=asset_id)
                    mat["blender_mcp_polyhaven_asset_id"] = asset_id
                    mat.use_nodes = True
                    nodes = mat.node_tree.nodes
                    links = mat.node_tree.links

                    # Clear default nodes
                    for node in nodes:
                        nodes.remove(node)

                    # Create output node
                    output = nodes.new(type="ShaderNodeOutputMaterial")
                    output.location = (300, 0)

                    # Create principled BSDF node
                    principled = nodes.new(type="ShaderNodeBsdfPrincipled")
                    principled.location = (0, 0)
                    links.new(principled.outputs[0], output.inputs[0])

                    # Add texture nodes based on available maps
                    tex_coord = nodes.new(type="ShaderNodeTexCoord")
                    tex_coord.location = (-800, 0)

                    mapping = nodes.new(type="ShaderNodeMapping")
                    mapping.location = (-600, 0)
                    mapping.vector_type = "TEXTURE"  # Changed from default 'POINT' to 'TEXTURE'
                    links.new(tex_coord.outputs["UV"], mapping.inputs["Vector"])

                    # Position offset for texture nodes
                    x_pos = -400
                    y_pos = 300

                    # Connect different texture maps
                    for map_type, image in downloaded_maps.items():
                        tex_node = nodes.new(type="ShaderNodeTexImage")
                        tex_node.location = (x_pos, y_pos)
                        tex_node.image = image

                        # Set color space based on map type
                        if map_type.lower() in {"color", "diffuse", "albedo"}:
                            with suppress(Exception):
                                tex_node.image.colorspace_settings.name = "sRGB"  # Use default if sRGB not available
                        else:
                            with suppress(Exception):
                                tex_node.image.colorspace_settings.name = (
                                    "Non-Color"  # Use default if Non-Color not available
                                )

                        links.new(mapping.outputs["Vector"], tex_node.inputs["Vector"])

                        # Connect to appropriate input on Principled BSDF
                        if map_type.lower() in {"color", "diffuse", "albedo"}:
                            links.new(
                                tex_node.outputs["Color"],
                                principled.inputs["Base Color"],
                            )
                        elif map_type.lower() in {"roughness", "rough"}:
                            links.new(
                                tex_node.outputs["Color"],
                                principled.inputs["Roughness"],
                            )
                        elif map_type.lower() in {"metallic", "metalness", "metal"}:
                            links.new(tex_node.outputs["Color"], principled.inputs["Metallic"])
                        elif map_type.lower() in {"normal", "nor"}:
                            # Add normal map node
                            normal_map = nodes.new(type="ShaderNodeNormalMap")
                            normal_map.location = (x_pos + 200, y_pos)
                            links.new(tex_node.outputs["Color"], normal_map.inputs["Color"])
                            links.new(
                                normal_map.outputs["Normal"],
                                principled.inputs["Normal"],
                            )
                        elif map_type in {"displacement", "disp", "height"}:
                            # Add displacement node
                            disp_node = nodes.new(type="ShaderNodeDisplacement")
                            disp_node.location = (x_pos + 200, y_pos - 200)
                            links.new(tex_node.outputs["Color"], disp_node.inputs["Height"])
                            links.new(
                                disp_node.outputs["Displacement"],
                                output.inputs["Displacement"],
                            )

                        y_pos -= 250

                    return {
                        "success": True,
                        "message": f"Texture {asset_id} imported as material",
                        "material": mat.name,
                        "maps": [image.name for image in downloaded_maps.values()],
                        "map_types": list(downloaded_maps),
                    }

                except Exception as e:
                    return {"error": f"Failed to process textures: {sanitize_blender_error(e)}"}

            elif asset_type == "models":
                # For models, prefer glTF format if available
                if not file_format:
                    file_format = "gltf"  # Default format for models

                if file_format in files_data and resolution in files_data[file_format]:
                    file_info = files_data[file_format][resolution][file_format]
                    file_url = file_info["url"]

                    # Create a temporary directory to store the model and its dependencies
                    temp_dir = tempfile.mkdtemp()
                    main_file_path = ""

                    try:
                        # Download the main model file
                        main_file_name = file_url.split("/")[-1]
                        main_file_path = os.path.join(temp_dir, main_file_name)

                        download_file(file_url, main_file_path, headers=REQ_HEADERS, max_bytes=_MAX_MODEL_FILE_BYTES)

                        # Check for included files and download them
                        if file_info.get("include"):
                            for include_path, include_info in file_info["include"].items():
                                # Get the URL for the included file - this is the fix
                                include_url = include_info["url"]

                                # Validate include_path — the API response controls these
                                # dict keys; a malicious or MITM'd response could request an
                                # absolute path or one containing ".." to escape temp_dir
                                # and write arbitrary files (e.g. ~/.bashrc, authorized_keys).
                                # Mirrors the zip-slip check in import_sketchfab_model.
                                target_path = os.path.join(temp_dir, os.path.normpath(include_path))
                                abs_temp_dir = os.path.abspath(temp_dir)
                                abs_target_path = os.path.abspath(target_path)
                                if (
                                    os.path.isabs(include_path)
                                    or ".." in include_path
                                    or not abs_target_path.startswith(abs_temp_dir + os.sep)
                                ):
                                    print(f"Skipping include with unsafe path: {include_path}")
                                    continue

                                # Create the directory structure for the included file
                                include_file_path = target_path
                                os.makedirs(os.path.dirname(include_file_path), exist_ok=True)

                                # Download the included file
                                download_file(
                                    include_url,
                                    include_file_path,
                                    headers=REQ_HEADERS,
                                    max_bytes=_MAX_IMAGE_BYTES,
                                )

                        # Import the model into Blender
                        before_ids = {obj.session_uid for obj in bpy.data.objects}
                        operator_result = None
                        if file_format in {"gltf", "glb"}:
                            operator_result = bpy.ops.import_scene.gltf(filepath=main_file_path)
                        elif file_format == "fbx":
                            operator_result = bpy.ops.import_scene.fbx(filepath=main_file_path)
                        elif file_format == "obj":
                            operator_result = bpy.ops.wm.obj_import(filepath=main_file_path)
                        elif file_format == "blend":
                            validated = _validated_download(main_file_path, temp_dir)
                            # An appended object's Python driver runs when this preference is on.
                            _refuse_scripts_auto_execute("import_polyhaven_asset")
                            # `bpy.data.libraries.load` is a context manager at
                            # runtime; the stub declares it returning None.
                            with bpy.data.libraries.load(validated, link=False) as (  # pyright: ignore[reportGeneralTypeIssues]
                                data_from,
                                data_to,
                            ):
                                data_to.objects = data_from.objects

                            # Link the objects to the scene
                            for obj in data_to.objects:
                                if obj is not None:
                                    bpy.context.collection.objects.link(obj)
                        else:
                            return {"error": f"Unsupported model format: {file_format}"}
                        if operator_result is not None and "FINISHED" not in operator_result:
                            return {"error": f"Blender model import was cancelled: {operator_result}"}

                        imported_objects = [obj.name for obj in bpy.data.objects if obj.session_uid not in before_ids]
                        if not imported_objects:
                            return {"error": "Blender imported no objects from the downloaded model"}

                        return {
                            "success": True,
                            "message": f"Model {asset_id} imported successfully",
                            "imported_objects": imported_objects,
                        }
                    except Exception as e:
                        # Blender's and the OS's error text name the temp file's absolute path.
                        return {"error": f"Failed to import model: {sanitize_blender_error(e)}"}
                    finally:
                        # Clean up temporary directory
                        with suppress(Exception):
                            shutil.rmtree(temp_dir)
                else:
                    return {"error": "Requested format or resolution not available for this model"}

            else:
                return {"error": f"Unsupported asset type: {asset_type}"}

        except Exception as e:
            return {"error": f"Failed to download asset: {sanitize_blender_error(e)}"}

    def apply_polyhaven_texture(
        self,
        object_name,
        texture_id,
        replacement_policy="APPEND",
        material_slot_index=None,
        confirm_replace_all=False,
    ):
        """Assign the material created by import_polyhaven_asset without rebuilding its graph."""
        obj = bpy.data.objects.get(object_name)
        if obj is None:
            raise ValueError(f"Object not found: {object_name}")
        if obj.data is None or not hasattr(obj.data, "materials"):
            raise ValueError(f"Object '{object_name}' cannot accept materials")
        material = next(
            (item for item in bpy.data.materials if item.get("blender_mcp_polyhaven_asset_id") == texture_id),
            bpy.data.materials.get(texture_id),
        )
        if material is None:
            raise ValueError(
                f"Imported Poly Haven material not found: {texture_id}. "
                "Call import_polyhaven_asset with asset_type='textures' first."
            )

        policy = str(replacement_policy).upper()
        slots = obj.data.materials
        if policy == "APPEND":
            existing_index = next((index for index, item in enumerate(slots) if item == material), None)
            if existing_index is None:
                slots.append(material)
                slot_index = len(slots) - 1
            else:
                slot_index = existing_index
        elif policy == "REPLACE_SLOT":
            if material_slot_index is None or not 0 <= material_slot_index < len(slots):
                raise ValueError("REPLACE_SLOT requires a valid material_slot_index")
            obj.material_slots[material_slot_index].material = material
            slot_index = material_slot_index
        elif policy == "REPLACE_ALL":
            if not confirm_replace_all:
                raise ValueError("confirm_replace_all=True is required for REPLACE_ALL")
            slots.clear()
            slots.append(material)
            slot_index = 0
        else:
            raise ValueError("replacement_policy must be APPEND, REPLACE_SLOT, or REPLACE_ALL")

        images = (
            sorted(
                {
                    node.image.name
                    for node in material.node_tree.nodes
                    if getattr(node, "type", None) == "TEX_IMAGE" and getattr(node, "image", None) is not None
                }
            )
            if material.use_nodes and material.node_tree
            else []
        )
        bpy.context.view_layer.update()
        return {
            "success": True,
            "message": f"Assigned material {material.name} to {obj.name}",
            "material": material.name,
            "maps": images,
            "slot_index": slot_index,
            "replacement_policy": policy,
            "material_info": {
                "name": material.name,
                "has_nodes": material.use_nodes,
                "node_count": len(material.node_tree.nodes) if material.use_nodes and material.node_tree else 0,
            },
        }

    def get_polyhaven_status(self):
        """
        Get the current status of PolyHaven integration.

        Returns:
            Result produced by the operation.

        """
        enabled = bpy.context.scene.blendermcp_use_polyhaven
        if enabled:
            return {
                "enabled": True,
                "message": "PolyHaven integration is enabled and ready to use.",
            }
        else:
            return {
                "enabled": False,
                "message": """PolyHaven integration is currently disabled. To enable it:
                            1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                            2. Check the 'Use assets from Poly Haven' checkbox
                            3. Restart the connection to Claude""",
            }
