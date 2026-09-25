import os
import shutil
import string
import tempfile

from contextlib import suppress

import bpy

from ..constants import REQ_HEADERS
from ..file_paths import PathOutsideRootsError, resolve_blend_path, sanitize_blender_error
from ..helpers import counted_page
from ..network import download_file, get_json
from .blend_files import refuse_scripts_auto_execute

_MAX_IMAGE_BYTES = 512 * 1024 * 1024
_MAX_MODEL_FILE_BYTES = 2 * 1024 * 1024 * 1024
# The maps that carry colour rather than data, so only these are read as sRGB.
_COLOR_MAP_TYPES = frozenset({"color", "diffuse", "albedo"})
# Keys in a files response that are whole scenes, not texture maps.
_NON_TEXTURE_KEYS = frozenset({"blend", "gltf"})
# A resolution reaches a cache file's name, so it may hold nothing else.
_SAFE_FILENAME_CHARACTERS = frozenset(string.ascii_letters + string.digits + "-_")


def _validated_download(path: str, download_dir: str) -> str:
    """
    Check a downloaded `.blend` before Blender parses it.

    The download is untrusted. The boundary is the handler's own temp directory,
    not the deployment's file roots, because no caller named this path, so the
    shared refusal - which points at BLENDERMCP_FILE_ROOTS - is replaced here.

    Args:
        path: Where the download was written.
        download_dir: The temp directory the handler created for it.

    Returns:
        str: The canonical path to load.

    Raises:
        ValueError: If the file is not a `.blend` or resolves outside the directory.

    """
    try:
        return resolve_blend_path(path, roots=[download_dir], must_exist=True)
    except PathOutsideRootsError:
        raise ValueError("downloaded file resolves outside its download directory") from None


def _has_safe_filename_characters(value: object) -> bool:
    """
    Report whether a value may go into a cache file's name unchanged.

    Args:
        value: What the client sent as the resolution.

    Returns:
        bool: True for a non-empty string of letters, digits, `-` and `_`.

    """
    return isinstance(value, str) and bool(value) and all(c in _SAFE_FILENAME_CHARACTERS for c in value)


def _filename_component(asset_id) -> str:
    """
    Reduce an asset id to the part of it that may become a file name.

    Args:
        asset_id: The asset id the client sent.

    Returns:
        str: Letters, digits, `-` and `_`, with anything else replaced by `_`
        and the result stripped of leading and trailing underscores. Empty when
        the id contributes nothing usable.

    """
    return "".join(
        character if character.isalnum() or character in {"-", "_"} else "_" for character in asset_id
    ).strip("_")


def _cached_image_path(safe_asset_id: str, resolution: str, file_format: str) -> str | None:
    """
    Name the file an HDRI is cached at, creating the cache directory.

    Cached rather than temporary because an HDRI is referenced by path from the
    world it lights: a temp file would leave the world pointing at nothing.

    Args:
        safe_asset_id: `_filename_component`'s result, already known non-empty.
        resolution: The requested resolution, already charset-checked.
        file_format: `hdr` or `exr`.

    Returns:
        str | None: The path, or None when Blender has no writable data directory.

    """
    cache_directory = bpy.utils.user_resource(
        "DATAFILES",
        path=os.path.join("blender_mcp", "polyhaven"),
        create=True,
    )
    if not cache_directory:
        return None
    return os.path.join(cache_directory, f"{safe_asset_id}_{resolution}.{file_format}")


def _set_colorspace(image, *, color: bool) -> None:
    """
    Read one texture as colour or as data, tolerating a build without that profile.

    Args:
        image: The loaded `bpy.types.Image`.
        color: True for a base-colour map, False for roughness, normals and the rest.

    """
    with suppress(Exception):
        image.colorspace_settings.name = "sRGB" if color else "Non-Color"


def _packed_map_image(path: str, name: str, *, color: bool):
    """
    Load one downloaded map into the file itself, so the material outlives the temp file.

    Args:
        path: The downloaded file.
        name: The datablock name to publish it under.
        color: Passed to `_set_colorspace`.

    Returns:
        bpy.types.Image: The packed image.

    """
    image = bpy.data.images.load(path)
    image.name = name
    image.pack()
    _set_colorspace(image, color=color)
    return image


def _downloaded_map(asset_id, map_type: str, file_format: str, file_url: str):
    """
    Download one texture map through a temp file that is always removed.

    Args:
        asset_id: The asset id, for the datablock name.
        map_type: Poly Haven's name for this map.
        file_format: The requested format, also the temp file's suffix.
        file_url: Where to fetch it.

    Returns:
        bpy.types.Image: The packed image.

    """
    with tempfile.NamedTemporaryFile(suffix=f".{file_format}", delete=False) as tmp_file:
        tmp_path = tmp_file.name
    try:
        download_file(file_url, tmp_path, headers=REQ_HEADERS, max_bytes=_MAX_IMAGE_BYTES)
        return _packed_map_image(tmp_path, f"{asset_id}_{map_type}.{file_format}", color=map_type in _COLOR_MAP_TYPES)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(tmp_path)


def _downloaded_texture_maps(asset_id, files_data, resolution, file_format: str) -> dict:
    """
    Download every map this asset publishes at the requested resolution and format.

    Args:
        asset_id: The asset id.
        files_data: Poly Haven's files response.
        resolution: The requested resolution.
        file_format: The requested format.

    Returns:
        dict: Map type -> packed image; empty when the asset publishes none.

    """
    downloaded_maps = {}
    for map_type in files_data:
        if map_type in _NON_TEXTURE_KEYS:
            continue
        if resolution not in files_data[map_type] or file_format not in files_data[map_type][resolution]:
            continue
        downloaded_maps[map_type] = _downloaded_map(
            asset_id, map_type, file_format, files_data[map_type][resolution][file_format]["url"]
        )
    return downloaded_maps


def _connect_texture_map(nodes, links, map_type: str, tex_node, principled, output, x_pos: int, y_pos: int) -> None:
    """
    Wire one texture node into the shader input its map type belongs to.

    A map type the graph has no input for is downloaded and packed but left
    unconnected, which is how an asset can carry more maps than a Principled
    BSDF takes.

    Args:
        nodes: The material's node collection, for the nodes a map needs beside it.
        links: The material's link collection.
        map_type: Poly Haven's name for this map.
        tex_node: The image node already placed for it.
        principled: The Principled BSDF node.
        output: The material output node.
        x_pos: Where the image node sits, for the nodes placed beside it.
        y_pos: The row this map occupies.

    """
    normalized = map_type.lower()
    if normalized in _COLOR_MAP_TYPES:
        links.new(tex_node.outputs["Color"], principled.inputs["Base Color"])
    elif normalized in {"roughness", "rough"}:
        links.new(tex_node.outputs["Color"], principled.inputs["Roughness"])
    elif normalized in {"metallic", "metalness", "metal"}:
        links.new(tex_node.outputs["Color"], principled.inputs["Metallic"])
    elif normalized in {"normal", "nor"}:
        normal_map = nodes.new(type="ShaderNodeNormalMap")
        normal_map.location = (x_pos + 200, y_pos)
        links.new(tex_node.outputs["Color"], normal_map.inputs["Color"])
        links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])
    elif map_type in {"displacement", "disp", "height"}:
        disp_node = nodes.new(type="ShaderNodeDisplacement")
        disp_node.location = (x_pos + 200, y_pos - 200)
        links.new(tex_node.outputs["Color"], disp_node.inputs["Height"])
        links.new(disp_node.outputs["Displacement"], output.inputs["Displacement"])


def _texture_material(asset_id, downloaded_maps: dict):
    """
    Build the material the downloaded maps describe, one UV-mapped image node per map.

    Args:
        asset_id: The asset id, which names the material.
        downloaded_maps: `_downloaded_texture_maps`' result, never empty.

    Returns:
        bpy.types.Material: The new material.

    """
    material = bpy.data.materials.new(name=asset_id)
    material["blender_mcp_polyhaven_asset_id"] = asset_id
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    for node in list(nodes):
        nodes.remove(node)
    output = nodes.new(type="ShaderNodeOutputMaterial")
    output.location = (300, 0)
    principled = nodes.new(type="ShaderNodeBsdfPrincipled")
    principled.location = (0, 0)
    links.new(principled.outputs[0], output.inputs[0])
    tex_coord = nodes.new(type="ShaderNodeTexCoord")
    tex_coord.location = (-800, 0)
    mapping = nodes.new(type="ShaderNodeMapping")
    mapping.location = (-600, 0)
    # Blender's default is POINT, which does not tile a texture across a surface.
    mapping.vector_type = "TEXTURE"
    links.new(tex_coord.outputs["UV"], mapping.inputs["Vector"])
    x_pos, y_pos = -400, 300
    for map_type, image in downloaded_maps.items():
        tex_node = nodes.new(type="ShaderNodeTexImage")
        tex_node.location = (x_pos, y_pos)
        tex_node.image = image
        _set_colorspace(tex_node.image, color=map_type.lower() in _COLOR_MAP_TYPES)
        links.new(mapping.outputs["Vector"], tex_node.inputs["Vector"])
        _connect_texture_map(nodes, links, map_type, tex_node, principled, output, x_pos, y_pos)
        y_pos -= 250
    return material


def _texture_material_reply(asset_id, files_data, resolution, file_format: str) -> dict:
    """
    Download an asset's maps and describe the material they were built into.

    Args:
        asset_id: The asset id.
        files_data: Poly Haven's files response.
        resolution: The requested resolution.
        file_format: The requested format.

    Returns:
        dict: `success`, `message`, `material`, `maps` and `map_types`, or
        `error` when the asset publishes no map at that resolution and format.

    """
    downloaded_maps = _downloaded_texture_maps(asset_id, files_data, resolution, file_format)
    if not downloaded_maps:
        return {"error": "No texture maps found for the requested resolution and format"}
    material = _texture_material(asset_id, downloaded_maps)
    return {
        "success": True,
        "message": f"Texture {asset_id} imported as material",
        "material": material.name,
        "maps": [image.name for image in downloaded_maps.values()],
        "map_types": list(downloaded_maps),
    }


def _import_textures(asset_id, files_data, resolution, file_format) -> dict:
    """
    Import one Poly Haven texture set as a material.

    Args:
        asset_id: The asset id.
        files_data: Poly Haven's files response.
        resolution: The requested resolution.
        file_format: The requested format; `jpg` when the client named none.

    Returns:
        dict: `_texture_material_reply`'s report, or `error` with no filesystem path.

    """
    try:
        return _texture_material_reply(asset_id, files_data, resolution, file_format or "jpg")
    except Exception as e:
        return {"error": f"Failed to process textures: {sanitize_blender_error(e)}"}


def _safe_include_path(include_path: str, temp_dir: str) -> str | None:
    """
    Place one of a model's included files inside the download directory, or refuse it.

    The API response controls these dict keys; a malicious or MITM'd response
    could request an absolute path or one containing ".." to escape `temp_dir`
    and write arbitrary files (e.g. ~/.bashrc, authorized_keys). Mirrors the
    zip-slip check in import_sketchfab_model.

    Args:
        include_path: The relative path the response asked for.
        temp_dir: The download directory it must stay inside.

    Returns:
        str | None: Where to write it, or None when it escapes the directory.

    """
    target_path = os.path.join(temp_dir, os.path.normpath(include_path))
    abs_temp_dir = os.path.abspath(temp_dir)
    abs_target_path = os.path.abspath(target_path)
    if os.path.isabs(include_path) or ".." in include_path or not abs_target_path.startswith(abs_temp_dir + os.sep):
        return None
    return target_path


def _downloaded_model_files(file_info: dict, temp_dir: str) -> str:
    """
    Download a model and every file it includes into one directory.

    Args:
        file_info: The chosen format's entry in Poly Haven's files response.
        temp_dir: The download directory.

    Returns:
        str: The main model file, for the importer to read.

    """
    file_url = file_info["url"]
    main_file_path = os.path.join(temp_dir, file_url.split("/")[-1])
    download_file(file_url, main_file_path, headers=REQ_HEADERS, max_bytes=_MAX_MODEL_FILE_BYTES)
    for include_path, include_info in (file_info.get("include") or {}).items():
        include_file_path = _safe_include_path(include_path, temp_dir)
        if include_file_path is None:
            print(f"Skipping include with unsafe path: {include_path}")
            continue
        os.makedirs(os.path.dirname(include_file_path), exist_ok=True)
        download_file(include_info["url"], include_file_path, headers=REQ_HEADERS, max_bytes=_MAX_IMAGE_BYTES)
    return main_file_path


def _append_downloaded_blend(main_file_path: str, temp_dir: str) -> None:
    """
    Append the objects in a downloaded `.blend`, after checking it is one.

    Args:
        main_file_path: The downloaded file.
        temp_dir: The directory it must resolve inside.

    Raises:
        ValueError: When the download is not a `.blend`, resolves outside its
            directory, or Blender is set to run scripts embedded in a file.

    """
    validated = _validated_download(main_file_path, temp_dir)
    # An appended object's Python driver runs when this preference is on.
    refuse_scripts_auto_execute("import_polyhaven_asset")
    # `bpy.data.libraries.load` is a context manager at runtime; the stub
    # declares it returning None.
    with bpy.data.libraries.load(validated, link=False) as (  # pyright: ignore[reportGeneralTypeIssues]
        data_from,
        data_to,
    ):
        data_to.objects = data_from.objects
    for obj in data_to.objects:
        if obj is not None:
            bpy.context.collection.objects.link(obj)


def _run_model_import(file_format: str, main_file_path: str, temp_dir: str) -> dict | None:
    """
    Hand the downloaded model to the importer for its format.

    Args:
        file_format: The format that was downloaded.
        main_file_path: The downloaded file.
        temp_dir: The download directory, for the `.blend` path check.

    Returns:
        dict | None: None when the import ran, else the `error` reply for an
        unsupported format or an operator that cancelled.

    """
    if file_format in {"gltf", "glb"}:
        operator_result = bpy.ops.import_scene.gltf(filepath=main_file_path)
    elif file_format == "fbx":
        operator_result = bpy.ops.import_scene.fbx(filepath=main_file_path)
    elif file_format == "obj":
        operator_result = bpy.ops.wm.obj_import(filepath=main_file_path)
    elif file_format == "blend":
        _append_downloaded_blend(main_file_path, temp_dir)
        return None
    else:
        return {"error": f"Unsupported model format: {file_format}"}
    if "FINISHED" not in operator_result:
        return {"error": f"Blender model import was cancelled: {operator_result}"}
    return None


def _imported_model_reply(asset_id, file_info: dict, file_format: str, temp_dir: str) -> dict:
    """
    Download, import and describe one model, by the objects it added.

    Args:
        asset_id: The asset id.
        file_info: The chosen format's entry in Poly Haven's files response.
        file_format: The format being imported.
        temp_dir: The download directory.

    Returns:
        dict: `success`, `message`, `imported_objects` (counted by type beside a sample of
        names) and `changed_objects` - the imported roots, which no other imported object
        parents - or `error`.

    """
    main_file_path = _downloaded_model_files(file_info, temp_dir)
    before_ids = {obj.session_uid for obj in bpy.data.objects}
    refusal = _run_model_import(file_format, main_file_path, temp_dir)
    if refusal is not None:
        return refusal
    imported = [obj for obj in bpy.data.objects if obj.session_uid not in before_ids]
    if not imported:
        return {"error": "Blender imported no objects from the downloaded model"}
    imported_ids = {obj.session_uid for obj in imported}
    return {
        "success": True,
        "message": f"Model {asset_id} imported successfully",
        "imported_objects": counted_page(imported, type_of=lambda obj: obj.type, name_of=lambda obj: obj.name),
        # A model is moved or scaled by its roots; the members under them are counted above.
        "changed_objects": [
            obj.name for obj in imported if obj.parent is None or obj.parent.session_uid not in imported_ids
        ],
    }


def _import_model(asset_id, files_data, resolution, file_format) -> dict:
    """
    Import one Poly Haven model, with its included files, from a temp directory.

    Args:
        asset_id: The asset id.
        files_data: Poly Haven's files response.
        resolution: The requested resolution.
        file_format: The requested format; glTF when the client named none.

    Returns:
        dict: `_imported_model_reply`'s report, or `error` with no filesystem path.

    """
    file_format = file_format or "gltf"
    if file_format not in files_data or resolution not in files_data[file_format]:
        return {"error": "Requested format or resolution not available for this model"}
    temp_dir = tempfile.mkdtemp()
    try:
        return _imported_model_reply(asset_id, files_data[file_format][resolution][file_format], file_format, temp_dir)
    except Exception as e:
        # Blender's and the OS's error text name the temp file's absolute path.
        return {"error": f"Failed to import model: {sanitize_blender_error(e)}"}
    finally:
        with suppress(Exception):
            shutil.rmtree(temp_dir)


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

    def _configured_environment(self, image_path: str) -> dict:
        """
        Point the scene's world at a cached HDRI, creating the world when there is none.

        Args:
            image_path: The cached image to light the scene with.

        Returns:
            dict: `configure_hdri_environment`'s report.

        """
        scene = bpy.context.scene
        return self.configure_hdri_environment(
            scene_name=scene.name,
            image_path=image_path,
            strength=1.0,
            rotation=0.0,
            projection="EQUIRECTANGULAR",
            replacement_policy="REPLACE_MANAGED",
            world_name="World" if scene.world is None else None,
            create_world=scene.world is None,
        )

    def _downloaded_hdri_world(self, asset_id, file_url: str, persistent_path: str) -> dict:
        """
        Download one HDRI into the cache and light the scene with it.

        Written to `<target>.part` and renamed, so an interrupted download never
        leaves a half-file where the world expects an image; the partial name is
        removed whether the download succeeded or not.

        Args:
            asset_id: The asset id, for the message.
            file_url: Where to fetch the image.
            persistent_path: The cache file to write.

        Returns:
            dict: `success`, `message`, `image_name`, `image_path` and `world`,
            or `error` with no filesystem path.

        """
        partial_path = f"{persistent_path}.part"
        try:
            download_file(file_url, partial_path, headers=REQ_HEADERS, max_bytes=_MAX_IMAGE_BYTES)
            os.replace(partial_path, persistent_path)
            configured = self._configured_environment(persistent_path)
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

    def _import_hdri(self, asset_id, files_data, resolution, file_format) -> dict:
        """
        Import one Poly Haven HDRI as the scene's environment.

        Args:
            asset_id: The asset id.
            files_data: Poly Haven's files response.
            resolution: The requested resolution, which reaches the cache file's name.
            file_format: `hdr` or `exr`; `hdr` when the client named none.

        Returns:
            dict: `_downloaded_hdri_world`'s report, or `error`.

        """
        file_format = (file_format or "hdr").lower()
        if file_format not in {"hdr", "exr"}:
            return {"error": "Poly Haven HDRIs require file_format 'hdr' or 'exr'"}
        if not _has_safe_filename_characters(resolution):
            return {"error": "resolution contains unsupported filename characters"}
        if not (
            "hdri" in files_data and resolution in files_data["hdri"] and file_format in files_data["hdri"][resolution]
        ):
            return {"error": "Requested resolution or format not available for this HDRI"}
        safe_asset_id = _filename_component(asset_id)
        if not safe_asset_id:
            return {"error": "asset_id does not contain a safe filename component"}
        persistent_path = _cached_image_path(safe_asset_id, resolution, file_format)
        if persistent_path is None:
            return {"error": "Could not create the Blender MCP Poly Haven cache directory"}
        file_url = files_data["hdri"][resolution][file_format]["url"]
        return self._downloaded_hdri_world(asset_id, file_url, persistent_path)

    def _imported_asset(self, asset_id, asset_type, files_data, resolution, file_format) -> dict:
        """
        Route one asset to the importer for its type.

        Args:
            asset_id: The asset id.
            asset_type: `hdris`, `textures` or `models`.
            files_data: Poly Haven's files response.
            resolution: The requested resolution.
            file_format: The requested format, or None for the type's default.

        Returns:
            dict: That importer's report, or `error` for an unknown type.

        """
        if asset_type == "hdris":
            return self._import_hdri(asset_id, files_data, resolution, file_format)
        if asset_type == "textures":
            return _import_textures(asset_id, files_data, resolution, file_format)
        if asset_type == "models":
            return _import_model(asset_id, files_data, resolution, file_format)
        return {"error": f"Unsupported asset type: {asset_type}"}

    def import_polyhaven_asset(self, asset_id, asset_type, resolution="1k", file_format=None):
        """
        Download one Poly Haven asset and bring it into the open file.

        Args:
            asset_id: The Poly Haven asset id.
            asset_type: `hdris`, `textures` or `models`.
            resolution: The resolution Poly Haven publishes the asset at.
            file_format: The format to fetch; each asset type has its own default.

        Returns:
            dict: The report for the asset's type, or `error`. Blender's and the
            OS's error text names the temp file, so every failure is sanitized.

        """
        try:
            files_data = get_json(f"https://api.polyhaven.com/files/{asset_id}", headers=REQ_HEADERS)
            return self._imported_asset(asset_id, asset_type, files_data, resolution, file_format)
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
