"""
Poly Haven handlers: browse the catalog and import assets without blocking Blender's main thread.

Every network request runs in a `provider_fetches.REGISTRY` job on a worker thread. The
`start_polyhaven_*` commands validate on the main thread and start that job; a catalog
query's result rides on its fetch status, and a download's files wait in the fetch's
directory until `import_polyhaven_asset(fetch_id)` takes them and does the `bpy` work.
"""

import os
import shutil
import string

from contextlib import suppress

import bpy

from ..constants import REQ_HEADERS
from ..file_paths import PathOutsideRootsError, resolve_blend_path, sanitize_blender_error
from ..helpers import counted_page
from ..network import download_file, get_json
from ..provider_fetches import REGISTRY, FetchContext, ProviderFetchHandlersMixin
from .blend_files import refuse_scripts_auto_execute

API_ROOT = "https://api.polyhaven.com"

_MAX_IMAGE_BYTES = 512 * 1024 * 1024
_MAX_MODEL_FILE_BYTES = 2 * 1024 * 1024 * 1024
# The largest catalog page one call returns.
_MAX_CATALOG_PAGE = 100
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


def _downloaded_texture_files(context: FetchContext, files_data: dict, resolution, file_format: str) -> dict:
    """
    Download every map this asset publishes at the requested resolution and format; worker thread only.

    Each map is written to its own numbered file in the fetch's directory, because the
    map names come from the API response and never reach a path.

    Args:
        context: The fetch's context, its directory and transfer.
        files_data: Poly Haven's files response.
        resolution: The requested resolution.
        file_format: The requested format.

    Returns:
        dict: Map type -> downloaded file; empty when the asset publishes none.

    """
    suffix = f".{file_format}" if _has_safe_filename_characters(file_format) else ""
    downloaded = {}
    for map_type, variants in files_data.items():
        if map_type in _NON_TEXTURE_KEYS or not isinstance(variants, dict):
            continue
        if resolution not in variants or file_format not in variants[resolution]:
            continue
        context.stage(f"downloading texture map {len(downloaded) + 1}")
        path = os.path.join(context.directory, f"map_{len(downloaded)}{suffix}")
        download_file(
            variants[resolution][file_format]["url"],
            path,
            headers=REQ_HEADERS,
            max_bytes=_MAX_IMAGE_BYTES,
            transfer=context,
        )
        downloaded[map_type] = path
    return downloaded


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
        downloaded_maps: Map type -> packed image, never empty.

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


def _texture_material_reply(asset_id, map_paths: dict, file_format: str) -> dict:
    """
    Pack an asset's downloaded maps and describe the material they were built into.

    Args:
        asset_id: The asset id.
        map_paths: Map type -> downloaded file, never empty.
        file_format: The downloaded format, for the image names.

    Returns:
        dict: `success`, `message`, `material`, `maps` and `map_types`.

    """
    images = {
        map_type: _packed_map_image(path, f"{asset_id}_{map_type}.{file_format}", color=map_type in _COLOR_MAP_TYPES)
        for map_type, path in map_paths.items()
    }
    material = _texture_material(asset_id, images)
    return {
        "success": True,
        "message": f"Texture {asset_id} imported as material",
        "material": material.name,
        "maps": [image.name for image in images.values()],
        "map_types": list(images),
    }


def _import_textures(payload: dict) -> dict:
    """
    Import one downloaded Poly Haven texture set as a material.

    Args:
        payload: The download's payload: `asset_id`, `file_format` and `maps`.

    Returns:
        dict: `_texture_material_reply`'s report, or `error` with no filesystem path.

    """
    try:
        return _texture_material_reply(payload["asset_id"], payload["maps"], payload["file_format"])
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


def _downloaded_model_files(context: FetchContext, file_info: dict) -> str:
    """
    Download a model and every file it includes into the fetch's directory; worker thread only.

    Args:
        context: The fetch's context, its directory and transfer.
        file_info: The chosen format's entry in Poly Haven's files response.

    Returns:
        str: The main model file, for the importer to read.

    """
    temp_dir = context.directory
    file_url = file_info["url"]
    main_file_path = os.path.join(temp_dir, file_url.split("/")[-1])
    context.stage("downloading model")
    download_file(file_url, main_file_path, headers=REQ_HEADERS, max_bytes=_MAX_MODEL_FILE_BYTES, transfer=context)
    includes = file_info.get("include") or {}
    if includes:
        context.stage("downloading model includes")
    for include_path, include_info in includes.items():
        include_file_path = _safe_include_path(include_path, temp_dir)
        if include_file_path is None:
            print(f"Skipping include with unsafe path: {include_path}")
            continue
        os.makedirs(os.path.dirname(include_file_path), exist_ok=True)
        download_file(
            include_info["url"], include_file_path, headers=REQ_HEADERS, max_bytes=_MAX_IMAGE_BYTES, transfer=context
        )
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


def _imported_model_reply(asset_id, main_file_path: str, file_format: str, temp_dir: str) -> dict:
    """
    Import and describe one downloaded model, by the objects it added.

    Args:
        asset_id: The asset id.
        main_file_path: The downloaded main model file.
        file_format: The format being imported.
        temp_dir: The fetch's download directory.

    Returns:
        dict: `success`, `message`, `imported_objects` (counted by type beside a sample of
        names) and `changed_objects` - the imported roots, which no other imported object
        parents - or `error`.

    """
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


def _import_model(payload: dict, directory: str) -> dict:
    """
    Import one downloaded Poly Haven model, with its included files, from the fetch's directory.

    Args:
        payload: The download's payload: `asset_id`, `file_format` and `main_file`.
        directory: The fetch's directory, which the caller removes.

    Returns:
        dict: `_imported_model_reply`'s report, or `error` with no filesystem path.

    """
    try:
        return _imported_model_reply(payload["asset_id"], payload["main_file"], payload["file_format"], directory)
    except Exception as e:
        # Blender's and the OS's error text name the temp file's absolute path.
        return {"error": f"Failed to import model: {sanitize_blender_error(e)}"}


def _hdri_cache_request(asset_id, resolution, file_format) -> dict:
    """
    Validate an HDRI request and name its cache file; main thread, since it reads `bpy.utils`.

    Args:
        asset_id: The asset id, which reaches the cache file's name.
        resolution: The requested resolution, which reaches it too.
        file_format: `hdr` or `exr`; `hdr` when the client named none.

    Returns:
        dict: `file_format` and `persistent_path`, or `error`.

    """
    file_format = (file_format or "hdr").lower()
    if file_format not in {"hdr", "exr"}:
        return {"error": "Poly Haven HDRIs require file_format 'hdr' or 'exr'"}
    if not _has_safe_filename_characters(resolution):
        return {"error": "resolution contains unsupported filename characters"}
    safe_asset_id = _filename_component(asset_id)
    if not safe_asset_id:
        return {"error": "asset_id does not contain a safe filename component"}
    try:
        persistent_path = _cached_image_path(safe_asset_id, resolution, file_format)
    except Exception as e:
        return {"error": f"Failed to download asset: {sanitize_blender_error(e)}"}
    if persistent_path is None:
        return {"error": "Could not create the Blender MCP Poly Haven cache directory"}
    return {"file_format": file_format, "persistent_path": persistent_path}


def _catalog_page(assets: object, limit: int, offset: int) -> dict:
    """
    Order a catalog response by asset id and cut one page from it.

    Args:
        assets: Poly Haven's assets response.
        limit: The page size.
        offset: Where the page starts.

    Returns:
        dict: `assets`, `total_count`, `returned_count`, `offset`, `limit`,
        `truncated` and `next_offset`, or `error` for a response that is not a catalog.

    """
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


def _asset_download_payload(context: FetchContext, request: dict) -> dict:
    """
    Fetch one asset's file list and download what its type needs; the job, worker thread only.

    Args:
        context: The fetch's context, its directory and transfer.
        request: What the start command validated: `asset_id`, `asset_type`,
            `resolution`, `file_format`, and `persistent_path` for an HDRI.

    Returns:
        dict: The private payload `import_polyhaven_asset` takes, or `error`.

    """
    asset_id = request["asset_id"]
    asset_type = request["asset_type"]
    resolution = request["resolution"]
    context.stage("listing asset files")
    files_data = get_json(f"{API_ROOT}/files/{asset_id}", headers=REQ_HEADERS, transfer=context)
    payload = {"asset_id": asset_id, "asset_type": asset_type}
    if asset_type == "hdris":
        file_format = request["file_format"]
        if not (
            "hdri" in files_data and resolution in files_data["hdri"] and file_format in files_data["hdri"][resolution]
        ):
            return {"error": "Requested resolution or format not available for this HDRI"}
        path = os.path.join(context.directory, f"hdri.{file_format}")
        context.stage("downloading HDRI")
        download_file(
            files_data["hdri"][resolution][file_format]["url"],
            path,
            headers=REQ_HEADERS,
            max_bytes=_MAX_IMAGE_BYTES,
            transfer=context,
        )
        return {**payload, "file_format": file_format, "file": path, "persistent_path": request["persistent_path"]}
    if asset_type == "textures":
        file_format = request["file_format"] or "jpg"
        maps = _downloaded_texture_files(context, files_data, resolution, file_format)
        if not maps:
            return {"error": "No texture maps found for the requested resolution and format"}
        return {**payload, "file_format": file_format, "maps": maps}
    file_format = request["file_format"] or "gltf"
    if file_format not in files_data or resolution not in files_data[file_format]:
        return {"error": "Requested format or resolution not available for this model"}
    main_file = _downloaded_model_files(context, files_data[file_format][resolution][file_format])
    return {**payload, "file_format": file_format, "main_file": main_file}


class PolyhavenHandlersMixin(ProviderFetchHandlersMixin):
    """Provide handlers for browsing and importing Poly Haven assets, fetching on worker threads."""

    def start_polyhaven_categories(self, asset_type):
        """
        Start listing one asset type's Poly Haven categories on a worker thread.

        Args:
            asset_type: `hdris`, `textures`, `models` or `all`.

        Returns:
            dict: The fetch's status, whose `result` holds `categories` once it
            succeeds, or `error` for an invalid type.

        """
        if asset_type not in {"hdris", "textures", "models", "all"}:
            return {"error": f"Invalid asset type: {asset_type}. Must be one of: hdris, textures, models, all"}

        def job(context: FetchContext) -> dict:
            categories = get_json(f"{API_ROOT}/categories/{asset_type}", headers=REQ_HEADERS, transfer=context)
            return {"categories": categories}

        return REGISTRY.start(
            "polyhaven", "categories", job, keeps_files=False, failure_label="Failed to list Poly Haven categories"
        )

    def start_polyhaven_catalog(self, asset_type=None, categories=None, limit=20, offset=0):
        """
        Start fetching one page of the Poly Haven catalog on a worker thread.

        Args:
            asset_type: `hdris`, `textures`, `models`, `all` or None for every type.
            categories: Comma-separated categories to filter by.
            limit: The page size, 1 through 100.
            offset: Where the page starts in the catalog ordered by asset id.

        Returns:
            dict: The fetch's status, whose `result` holds `assets`, `total_count`,
            `returned_count`, `offset`, `limit`, `truncated` and `next_offset` once
            it succeeds, or `error` for an invalid argument.

        """
        params = {}
        if asset_type and asset_type != "all":
            if asset_type not in {"hdris", "textures", "models"}:
                return {"error": f"Invalid asset type: {asset_type}. Must be one of: hdris, textures, models, all"}
            params["type"] = asset_type
        if categories:
            params["categories"] = categories
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_CATALOG_PAGE:
            return {"error": f"limit must be an integer from 1 through {_MAX_CATALOG_PAGE}"}
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return {"error": "offset must be a non-negative integer"}

        def job(context: FetchContext) -> dict:
            assets = get_json(f"{API_ROOT}/assets", params=params, headers=REQ_HEADERS, transfer=context)
            return _catalog_page(assets, limit, offset)

        return REGISTRY.start(
            "polyhaven", "catalog", job, keeps_files=False, failure_label="Failed to list Poly Haven assets"
        )

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

    def _import_hdri(self, payload: dict) -> dict:
        """
        Move a downloaded HDRI into the cache and light the scene with it.

        The download sits in a fetch directory made inside the cache, so the rename
        is atomic: the world never sees a half-written image.

        Args:
            payload: The download's payload: `asset_id`, `file` and `persistent_path`.

        Returns:
            dict: `success`, `message`, `image_name`, `image_path` and `world`,
            or `error` with no filesystem path.

        """
        persistent_path = payload["persistent_path"]
        try:
            os.replace(payload["file"], persistent_path)
            configured = self._configured_environment(persistent_path)
            return {
                "success": True,
                "message": f"HDRI {payload['asset_id']} imported successfully",
                "image_name": configured["image"],
                "image_path": configured["image_path"],
                "world": configured["world"],
            }
        except Exception as e:
            return {"error": f"Failed to set up HDRI in Blender: {sanitize_blender_error(e)}"}

    def start_polyhaven_download(self, asset_id, asset_type, resolution="1k", file_format=None):
        """
        Start downloading one Poly Haven asset on a worker thread.

        Validation and the HDRI cache path (`bpy.utils`) happen here, on the main
        thread; the file list and every file are fetched by the job.

        Args:
            asset_id: The Poly Haven asset id.
            asset_type: `hdris`, `textures` or `models`.
            resolution: The resolution Poly Haven publishes the asset at.
            file_format: The format to fetch; each asset type has its own default.

        Returns:
            dict: The fetch's status; pass its `fetch_id` to `import_polyhaven_asset`
            once it SUCCEEDED. `error` for an invalid request.

        """
        if asset_type not in {"hdris", "textures", "models"}:
            return {"error": f"Unsupported asset type: {asset_type}"}
        request = {"asset_id": asset_id, "asset_type": asset_type, "resolution": resolution, "file_format": file_format}
        directory_parent = None
        if asset_type == "hdris":
            cached = _hdri_cache_request(asset_id, resolution, file_format)
            if "error" in cached:
                return cached
            request.update(cached)
            directory_parent = os.path.dirname(cached["persistent_path"])

        def job(context: FetchContext) -> dict:
            return _asset_download_payload(context, request)

        return REGISTRY.start(
            "polyhaven",
            "asset",
            job,
            keeps_files=True,
            failure_label="Failed to download asset",
            directory_parent=directory_parent,
        )

    def import_polyhaven_asset(self, fetch_id):
        """
        Bring one finished Poly Haven download into the open file; no network I/O.

        Args:
            fetch_id: The id `start_polyhaven_download` returned, once it SUCCEEDED.

        Returns:
            dict: The report for the asset's type, or `error`. Blender's and the
            OS's error text names the temp file, so every failure is sanitized.
            The fetch's directory is removed whatever happens.

        """
        payload, directory = REGISTRY.take(str(fetch_id), provider="polyhaven", kind="asset")
        try:
            asset_type = payload["asset_type"]
            if asset_type == "hdris":
                return self._import_hdri(payload)
            if asset_type == "textures":
                return _import_textures(payload)
            return _import_model(payload, directory or "")
        finally:
            if directory is not None:
                with suppress(Exception):
                    shutil.rmtree(directory)

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
