import base64
import json
import math
import os
import shutil
import stat
import zipfile

from contextlib import suppress
from http import HTTPStatus
from pathlib import PurePosixPath

import bpy
import mathutils
import requests

from ..network import download_file, get_bytes, get_json
from ..provider_fetches import REGISTRY, FetchCancelledError, FetchContext, ProviderFetchHandlersMixin

API_ROOT = "https://api.sketchfab.com/v3"

_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 20_000
_MAX_COMPRESSION_RATIO = 200
_MAX_PREVIEW_BYTES = 20 * 1024 * 1024
_PREVIEW_MIN_WIDTH = 400
_PREVIEW_MAX_WIDTH = 800
_MAX_SEARCH_COUNT = 100


def _validate_archive(zip_ref):
    """Reject traversal, links, archive bombs, and unreasonable member counts before extraction."""
    members = zip_ref.infolist()
    if len(members) > _MAX_ARCHIVE_MEMBERS:
        raise ValueError(f"Archive contains more than {_MAX_ARCHIVE_MEMBERS} members")
    total = 0
    for item in members:
        normalized = item.filename.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or (path.parts and ":" in path.parts[0]):
            raise ValueError(f"Unsafe archive path: {item.filename}")
        mode = item.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ValueError(f"Archive symlinks are not allowed: {item.filename}")
        total += item.file_size
        if total > _MAX_UNCOMPRESSED_BYTES:
            raise ValueError("Archive exceeds the uncompressed-size limit")
        if item.compress_size and item.file_size / item.compress_size > _MAX_COMPRESSION_RATIO:
            raise ValueError(f"Archive member has an unsafe compression ratio: {item.filename}")


def _world_mesh_bounds(objects):
    """Return combined world bounds and dimensions for imported mesh objects."""
    meshes = [obj for obj in objects if obj.type == "MESH"]
    if not meshes:
        return None, None
    minimum = mathutils.Vector((float("inf"), float("inf"), float("inf")))
    maximum = mathutils.Vector((float("-inf"), float("-inf"), float("-inf")))
    for obj in meshes:
        for corner in obj.bound_box:
            world = obj.matrix_world @ mathutils.Vector(corner)
            for axis in range(3):
                minimum[axis] = min(minimum[axis], world[axis])
                maximum[axis] = max(maximum[axis], world[axis])
    dimensions = [maximum[axis] - minimum[axis] for axis in range(3)]
    return [[*minimum], [*maximum]], dimensions


class _RefusedError(Exception):
    """A Sketchfab reply that ends a worker job with its message as the fetch's error."""


def _check_status(response, failure: str, uid: str | None = None) -> None:
    """
    Refuse a Sketchfab reply that is not a 200.

    Args:
        response: The `requests` response.
        failure: The message prefix for any other status, followed by the code.
        uid: The model UID, when a 404 means that model does not exist.

    Raises:
        _RefusedError: For a 401, a 404 when `uid` is given, or any other non-200.

    """
    if response.status_code == HTTPStatus.UNAUTHORIZED:
        raise _RefusedError("Authentication failed (401). Check your API key.")
    if uid is not None and response.status_code == HTTPStatus.NOT_FOUND:
        raise _RefusedError(f"Model not found: {uid}")
    if response.status_code != HTTPStatus.OK:
        raise _RefusedError(f"{failure} {response.status_code}")


def _check_account(api_key: str) -> dict:
    """
    Ask Sketchfab whom an API key belongs to; runs on a worker thread.

    Args:
        api_key: The key to check.

    Returns:
        dict: `{"enabled": bool, "message": str}`; a bad key or a timeout is a verdict, not a failure.

    """
    try:
        response = requests.get(f"{API_ROOT}/me", headers={"Authorization": f"Token {api_key}"}, timeout=30)
        if response.status_code == HTTPStatus.OK:
            username = response.json().get("username", "Unknown user")
            return {
                "enabled": True,
                "message": f"Sketchfab integration is enabled and ready to use. Logged in as: {username}",
            }
        return {
            "enabled": False,
            "message": f"Sketchfab API key seems invalid. Status code: {response.status_code}",
        }
    except requests.exceptions.Timeout:
        return {
            "enabled": False,
            "message": "Timeout connecting to Sketchfab API. Check your internet connection.",
        }
    except Exception as e:
        return {"enabled": False, "message": f"Error testing Sketchfab API key: {e!s}"}


def _search_page(api_key: str, endpoint: str, params: dict | None) -> dict:
    """
    Request one Sketchfab search page and check its shape.

    Args:
        api_key: The Sketchfab API key.
        endpoint: The search endpoint or a continuation URL.
        params: Query parameters, or None for a continuation URL.

    Returns:
        dict: Sketchfab's response (`results`, `next`, `previous`).

    Raises:
        _RefusedError: For a refused request or an empty or malformed response.

    """
    response = requests.get(endpoint, headers={"Authorization": f"Token {api_key}"}, params=params, timeout=30)
    _check_status(response, "API request failed with status code")
    response_data = response.json()
    if response_data is None:
        raise _RefusedError("Received empty response from Sketchfab API")
    if not isinstance(response_data.get("results", []), list):
        raise _RefusedError(f"Unexpected response format from Sketchfab API: {response_data}")
    return response_data


def _search(api_key: str, endpoint: str, params: dict | None) -> dict:
    """
    Request one Sketchfab search page; runs on a worker thread.

    Args:
        api_key: The Sketchfab API key.
        endpoint: The search endpoint or a continuation URL.
        params: Query parameters, or None for a continuation URL.

    Returns:
        dict: Sketchfab's response (`results`, `next`, `previous`), or `{"error": ...}`.

    """
    try:
        return _search_page(api_key, endpoint, params)
    except _RefusedError as e:
        return {"error": str(e)}
    except requests.exceptions.Timeout:
        return {"error": "Request timed out. Check your internet connection."}
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON response from Sketchfab API: {e!s}"}
    except Exception as e:
        return {"error": str(e)}


def _choose_thumbnail(data: dict) -> tuple[dict, str]:
    """
    Pick a model's medium thumbnail (~640px), else its first one.

    Args:
        data: The model's info.

    Returns:
        tuple[dict, str]: The thumbnail's record and its URL.

    Raises:
        _RefusedError: When the model has no thumbnail, or the chosen one has no URL.

    """
    thumbnails = data.get("thumbnails", {}).get("images", [])
    if not thumbnails:
        raise _RefusedError("No thumbnail available for this model")
    selected = next(
        (thumb for thumb in thumbnails if _PREVIEW_MIN_WIDTH <= thumb.get("width", 0) <= _PREVIEW_MAX_WIDTH),
        thumbnails[0],
    )
    url = selected.get("url")
    if not url:
        raise _RefusedError("Thumbnail URL not found")
    return selected, url


def _preview_result(context: FetchContext, api_key: str, uid: str) -> dict:
    """
    Fetch a model's info and thumbnail.

    Args:
        context: The fetch's progress and cancellation.
        api_key: The Sketchfab API key.
        uid: The model UID.

    Returns:
        dict: The base64 thumbnail and model details.

    """
    context.stage("requesting model info")
    response = requests.get(f"{API_ROOT}/models/{uid}", headers={"Authorization": f"Token {api_key}"}, timeout=30)
    _check_status(response, "Failed to get model info:", uid)
    data = response.json()
    thumbnail, thumbnail_url = _choose_thumbnail(data)
    context.stage("downloading thumbnail")
    image_bytes, content_type = get_bytes(thumbnail_url, max_bytes=_MAX_PREVIEW_BYTES, transfer=context)
    return {
        "success": True,
        "image_data": base64.b64encode(image_bytes).decode("ascii"),
        "format": "png" if "png" in content_type or thumbnail_url.endswith(".png") else "jpeg",
        "model_name": data.get("name", "Unknown"),
        "author": data.get("user", {}).get("username", "Unknown"),
        "uid": uid,
        "thumbnail_width": thumbnail.get("width"),
        "thumbnail_height": thumbnail.get("height"),
    }


def _preview(context: FetchContext, api_key: str, uid: str) -> dict:
    """
    Fetch a model's thumbnail as base64; runs on a worker thread.

    Args:
        context: The fetch's progress and cancellation.
        api_key: The Sketchfab API key.
        uid: The model UID.

    Returns:
        dict: The thumbnail and model details, or `{"error": ...}`.

    Raises:
        FetchCancelledError: When the fetch is cancelled mid-download.

    """
    try:
        return _preview_result(context, api_key, uid)
    except FetchCancelledError:
        raise
    except _RefusedError as e:
        return {"error": str(e)}
    except requests.exceptions.Timeout:
        return {"error": "Request timed out. Check your internet connection."}
    except Exception as e:
        return {"error": f"Failed to get model preview: {e!s}"}


def _extract_model(context: FetchContext, api_key: str, uid: str) -> dict:
    """
    Download one model's glTF archive into the fetch's directory and extract it.

    Args:
        context: The fetch's directory, progress, and cancellation.
        api_key: The Sketchfab API key.
        uid: The model UID.

    Returns:
        dict: The private payload `{"uid", "metadata", "main_file"}`.

    Raises:
        ValueError: When there is no glTF download, the archive is unsafe, or it holds no glTF file.

    """
    headers = {"Authorization": f"Token {api_key}"}
    directory = context.directory
    context.stage("requesting model metadata")
    metadata = get_json(f"{API_ROOT}/models/{uid}", headers=headers, transfer=context)
    context.stage("requesting download link")
    download = get_json(f"{API_ROOT}/models/{uid}/download", headers=headers, transfer=context)
    gltf = download.get("gltf") if isinstance(download, dict) else None
    download_url = gltf.get("url") if isinstance(gltf, dict) else None
    if not download_url:
        raise ValueError("No glTF download is available for this model")

    context.stage("downloading model")
    archive_path = os.path.join(directory, "model.zip")
    download_file(download_url, archive_path, max_bytes=_MAX_ARCHIVE_BYTES, transfer=context)
    context.stage("extracting model")
    with zipfile.ZipFile(archive_path, "r") as archive:
        _validate_archive(archive)
        archive.extractall(directory)
    context.check_cancelled()

    candidates = sorted(
        os.path.join(root, filename)
        for root, _directories, files in os.walk(directory)
        for filename in files
        if filename.lower().endswith((".gltf", ".glb"))
    )
    if not candidates:
        raise ValueError("No glTF file was found in the downloaded archive")
    main_file = next((path for path in candidates if path.lower().endswith(".gltf")), candidates[0])
    return {"uid": uid, "metadata": metadata, "main_file": main_file}


def _download_model(context: FetchContext, api_key: str, uid: str) -> dict:
    """
    Download and extract one model's glTF archive; runs on a worker thread.

    Args:
        context: The fetch's directory, progress, and cancellation.
        api_key: The Sketchfab API key.
        uid: The model UID.

    Returns:
        dict: The private payload `{"uid", "metadata", "main_file"}`, or `{"error": ...}`.

    """
    try:
        return _extract_model(context, api_key, uid)
    except (requests.exceptions.Timeout, ValueError, RuntimeError) as exc:
        return {"error": str(exc)}


class SketchfabHandlersMixin(ProviderFetchHandlersMixin):
    """
    Provide handlers for browsing and importing Sketchfab assets.

    Every Sketchfab request runs on a worker thread through `provider_fetches.REGISTRY`:
    the `start_*` commands only read the API key on the main thread, and
    `import_sketchfab_model` does only the `bpy` work on a finished download.
    """

    # region Sketchfab API
    def get_sketchfab_status(self):
        """
        Get the current status of Sketchfab integration without any network I/O.

        Returns:
            dict: `enabled` and `message`; with a key configured, also `verify_api_key`,
            telling the caller to run `start_sketchfab_account_check` for the verdict.

        """
        enabled = bpy.context.scene.blendermcp_use_sketchfab
        api_key = self.get_sketchfab_api_key()

        if enabled and api_key:
            return {
                "enabled": True,
                "message": "Sketchfab integration is enabled; checking its API key...",
                "verify_api_key": True,
            }
        elif enabled and not api_key:
            return {
                "enabled": False,
                "message": """Sketchfab integration is currently enabled, but API key is not given. To enable it:
                            1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                            2. Keep the 'Use Sketchfab' checkbox checked
                            3. Enter your Sketchfab API Key
                            4. Restart the connection to Claude""",
            }
        else:
            return {
                "enabled": False,
                "message": """Sketchfab integration is currently disabled. To enable it:
                            1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                            2. Check the 'Use assets from Sketchfab' checkbox
                            3. Enter your Sketchfab API Key
                            4. Restart the connection to Claude""",
            }

    def start_sketchfab_account_check(self):
        """
        Start checking the configured API key against Sketchfab's `/me` endpoint.

        Returns:
            dict: The fetch's status; its result is `{"enabled": bool, "message": str}`.

        """
        api_key = self.get_sketchfab_api_key()
        if not api_key:
            return {"error": "Sketchfab API key is not configured"}
        return REGISTRY.start(
            "sketchfab",
            "account",
            lambda _context: _check_account(api_key),
            keeps_files=False,
            failure_label="Failed to check the Sketchfab API key",
        )

    def start_sketchfab_search(self, query, categories=None, count=20, downloadable=True, cursor=None):
        """
        Start searching Sketchfab for models matching a query and optional filters.

        Args:
            query: Search query.
            categories: Optional comma-separated categories.
            count: Results per page, 1 through 100.
            downloadable: Only downloadable models when True.
            cursor: A continuation URL from an earlier page, or None.

        Returns:
            dict: The fetch's status, or `{"error": ...}` for invalid arguments; its
            result is Sketchfab's page (`results`, `next`, `previous`).

        """
        api_key = self.get_sketchfab_api_key()
        if not api_key:
            return {"error": "Sketchfab API key is not configured"}
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= _MAX_SEARCH_COUNT:
            return {"error": "count must be an integer from 1 through 100"}
        params = None
        endpoint = cursor or f"{API_ROOT}/search"
        if cursor:
            if not isinstance(cursor, str) or not cursor.startswith(f"{API_ROOT}/"):
                return {"error": "cursor must be a Sketchfab API continuation URL"}
        else:
            params = {
                "type": "models",
                "q": query,
                "count": count,
                "downloadable": downloadable,
                "archives_flavours": False,
            }
            if categories:
                params["categories"] = categories
        return REGISTRY.start(
            "sketchfab",
            "search",
            lambda _context: _search(api_key, endpoint, params),
            keeps_files=False,
            failure_label="Failed to search Sketchfab",
        )

    def start_sketchfab_preview(self, uid):
        """
        Start fetching a Sketchfab model's thumbnail.

        Args:
            uid: Model UID.

        Returns:
            dict: The fetch's status; its result holds base64 `image_data`, `format`,
            `model_name`, `author`, `uid`, and the thumbnail's size.

        """
        api_key = self.get_sketchfab_api_key()
        if not api_key:
            return {"error": "Sketchfab API key is not configured"}
        return REGISTRY.start(
            "sketchfab",
            "preview",
            lambda context: _preview(context, api_key, uid),
            keeps_files=False,
            failure_label="Failed to get model preview",
        )

    def start_sketchfab_download(self, uid):
        """
        Start downloading and extracting one model's glTF archive for `import_sketchfab_model`.

        Args:
            uid: Downloadable model UID.

        Returns:
            dict: The fetch's status.

        """
        api_key = self.get_sketchfab_api_key()
        if not api_key:
            return {"error": "Sketchfab API key is not configured"}
        return REGISTRY.start(
            "sketchfab",
            "model",
            lambda context: _download_model(context, api_key, uid),
            keeps_files=True,
            failure_label="Failed to download model",
        )

    def import_sketchfab_model(self, fetch_id, normalize_size=False, target_size=1.0):
        """
        Import a finished `start_sketchfab_download` fetch, optionally normalizing its size.

        Args:
            fetch_id: The finished download's id.
            normalize_size: Scale the model so its largest dimension is `target_size`.
            target_size: Target largest dimension in Blender units.

        Returns:
            dict: Imported and root object names, provenance, bounds and scale, or `{"error": ...}`.

        Raises:
            ValueError: For an invalid `target_size`; the fetch is left unconsumed.

        """
        if not isinstance(target_size, (int, float)) or isinstance(target_size, bool):
            raise ValueError("target_size must be a finite positive number")
        target_size = float(target_size)
        if not math.isfinite(target_size) or target_size <= 0:
            raise ValueError("target_size must be a finite positive number")

        directory = None
        try:
            payload, directory = REGISTRY.take(str(fetch_id), provider="sketchfab", kind="model")
            uid = payload["uid"]
            metadata = payload["metadata"]

            before_ids = {obj.session_uid for obj in bpy.data.objects}
            result = bpy.ops.import_scene.gltf(filepath=payload["main_file"])
            if "FINISHED" not in result:
                raise RuntimeError(f"Blender glTF import was cancelled: {result}")
            imported = [obj for obj in bpy.data.objects if obj.session_uid not in before_ids]
            if not imported:
                raise RuntimeError("Blender reported success but imported no objects")

            imported_set = set(imported)
            roots = [obj for obj in imported if obj.parent not in imported_set]
            bounds, dimensions = _world_mesh_bounds(imported)
            scale_applied = 1.0
            if normalize_size:
                if not dimensions or max(dimensions) <= 0:
                    raise ValueError("Imported model has no non-degenerate mesh bounds to normalize")
                scale_applied = target_size / max(dimensions)
                for root in roots:
                    root.scale = tuple(float(value) * scale_applied for value in root.scale)
                bpy.context.view_layer.update()
                bounds, dimensions = _world_mesh_bounds(imported)

            author = metadata.get("user", {}) if isinstance(metadata, dict) else {}
            license_data = metadata.get("license", {}) if isinstance(metadata, dict) else {}
            provenance = {
                "uid": uid,
                "name": metadata.get("name") if isinstance(metadata, dict) else None,
                "source_url": f"https://sketchfab.com/3d-models/{uid}",
                "author": author.get("displayName") or author.get("username"),
                "author_profile": author.get("profileUrl"),
                "license": license_data.get("label") or license_data.get("slug"),
                "license_url": license_data.get("url"),
                "attribution": metadata.get("attribution") if isinstance(metadata, dict) else None,
            }
            for obj in imported:
                obj["blender_mcp_source"] = "Sketchfab"
                obj["blender_mcp_source_uid"] = uid
                obj["blender_mcp_source_url"] = provenance["source_url"]
                if provenance["author"]:
                    obj["blender_mcp_author"] = provenance["author"]
                if provenance["license"]:
                    obj["blender_mcp_license"] = provenance["license"]

            response = {
                "success": True,
                "message": "Model imported successfully",
                "imported_objects": [obj.name for obj in imported],
                "root_objects": [obj.name for obj in roots],
                "provenance": provenance,
                "normalized": bool(normalize_size),
                "scale_applied": round(scale_applied, 6),
            }
            if bounds is not None and dimensions is not None:
                response["world_bounding_box"] = bounds
                response["dimensions"] = [round(value, 4) for value in dimensions]
            return response
        except (ValueError, RuntimeError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": f"Failed to import model: {exc!s}"}
        finally:
            if directory is not None:
                with suppress(Exception):
                    shutil.rmtree(directory)

    # endregion
