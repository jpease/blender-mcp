import json
import math
import os
import shutil
import stat
import tempfile
import zipfile

from contextlib import suppress
from pathlib import PurePosixPath

import bpy
import mathutils
import requests

from ..network import download_file, get_bytes, get_json

_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 20_000
_MAX_COMPRESSION_RATIO = 200
_MAX_PREVIEW_BYTES = 20 * 1024 * 1024


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


class SketchfabHandlersMixin:
    """Provide handlers for browsing and importing Sketchfab assets."""

    # region Sketchfab API
    def get_sketchfab_status(self):
        """
        Get the current status of Sketchfab integration.

        Returns:
            Result produced by the operation.

        """
        enabled = bpy.context.scene.blendermcp_use_sketchfab
        api_key = self.get_sketchfab_api_key()

        # Test the API key if present
        if api_key and enabled:
            try:
                headers = {"Authorization": f"Token {api_key}"}

                response = requests.get(
                    "https://api.sketchfab.com/v3/me",
                    headers=headers,
                    timeout=30,  # Add timeout of 30 seconds
                )

                if response.status_code == 200:
                    user_data = response.json()
                    username = user_data.get("username", "Unknown user")
                    return {
                        "enabled": True,
                        "message": f"Sketchfab integration is enabled and ready to use. Logged in as: {username}",
                    }
                else:
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
                return {
                    "enabled": False,
                    "message": f"Error testing Sketchfab API key: {e!s}",
                }

        if enabled and api_key:
            return {
                "enabled": True,
                "message": "Sketchfab integration is enabled and ready to use.",
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

    def search_sketchfab_models(self, query, categories=None, count=20, downloadable=True, cursor=None):
        """
        Search for models on Sketchfab based on query and optional filters.

        Args:
            query: Search query.
            categories: Value for categories.
            count: Value for count.
            downloadable: Value for downloadable.

        Returns:
            Result produced by the operation.

        """
        try:
            api_key = self.get_sketchfab_api_key()
            if not api_key:
                return {"error": "Sketchfab API key is not configured"}

            if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 100:
                return {"error": "count must be an integer from 1 through 100"}
            params = None
            endpoint = cursor or "https://api.sketchfab.com/v3/search"
            if cursor:
                if not isinstance(cursor, str) or not cursor.startswith("https://api.sketchfab.com/v3/"):
                    return {"error": "cursor must be a Sketchfab API continuation URL"}
            else:
                params = {
                    "type": "models",
                    "q": query,
                    "count": count,
                    "downloadable": downloadable,
                    "archives_flavours": False,
                }

            if categories and params is not None:
                params["categories"] = categories

            # Make API request to Sketchfab search endpoint
            # The proper format according to Sketchfab API docs for API key auth
            headers = {"Authorization": f"Token {api_key}"}

            # Use the search endpoint as specified in the API documentation
            response = requests.get(
                endpoint,
                headers=headers,
                params=params,
                timeout=30,  # Add timeout of 30 seconds
            )

            if response.status_code == 401:
                return {"error": "Authentication failed (401). Check your API key."}

            if response.status_code != 200:
                return {"error": f"API request failed with status code {response.status_code}"}

            response_data = response.json()

            # Safety check on the response structure
            if response_data is None:
                return {"error": "Received empty response from Sketchfab API"}

            # Handle 'results' potentially missing from response
            results = response_data.get("results", [])
            if not isinstance(results, list):
                return {"error": f"Unexpected response format from Sketchfab API: {response_data}"}

            return response_data

        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection."}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON response from Sketchfab API: {e!s}"}
        except Exception as e:
            import traceback

            traceback.print_exc()
            return {"error": str(e)}

    def get_sketchfab_model_preview(self, uid):
        """
        Get thumbnail preview image of a Sketchfab model by its UID.

        Args:
            uid: Value for uid.

        Returns:
            Result produced by the operation.

        """
        try:
            import base64

            api_key = self.get_sketchfab_api_key()
            if not api_key:
                return {"error": "Sketchfab API key is not configured"}

            headers = {"Authorization": f"Token {api_key}"}

            # Get model info which includes thumbnails
            response = requests.get(
                f"https://api.sketchfab.com/v3/models/{uid}",
                headers=headers,
                timeout=30,
            )

            if response.status_code == 401:
                return {"error": "Authentication failed (401). Check your API key."}

            if response.status_code == 404:
                return {"error": f"Model not found: {uid}"}

            if response.status_code != 200:
                return {"error": f"Failed to get model info: {response.status_code}"}

            data = response.json()
            thumbnails = data.get("thumbnails", {}).get("images", [])

            if not thumbnails:
                return {"error": "No thumbnail available for this model"}

            # Find a suitable thumbnail (prefer medium size ~640px)
            selected_thumbnail = None
            for thumb in thumbnails:
                width = thumb.get("width", 0)
                if 400 <= width <= 800:
                    selected_thumbnail = thumb
                    break

            # Fallback to the first available thumbnail
            if not selected_thumbnail:
                selected_thumbnail = thumbnails[0]

            thumbnail_url = selected_thumbnail.get("url")
            if not thumbnail_url:
                return {"error": "Thumbnail URL not found"}

            # Download the thumbnail image
            image_bytes, content_type = get_bytes(thumbnail_url, max_bytes=_MAX_PREVIEW_BYTES)

            # Encode image as base64
            image_data = base64.b64encode(image_bytes).decode("ascii")

            # Determine format from content type or URL
            img_format = "png" if "png" in content_type or thumbnail_url.endswith(".png") else "jpeg"

            # Get additional model info for context
            model_name = data.get("name", "Unknown")
            author = data.get("user", {}).get("username", "Unknown")

            return {
                "success": True,
                "image_data": image_data,
                "format": img_format,
                "model_name": model_name,
                "author": author,
                "uid": uid,
                "thumbnail_width": selected_thumbnail.get("width"),
                "thumbnail_height": selected_thumbnail.get("height"),
            }

        except requests.exceptions.Timeout:
            return {"error": "Request timed out. Check your internet connection."}
        except Exception as e:
            import traceback

            traceback.print_exc()
            return {"error": f"Failed to get model preview: {e!s}"}

    def import_sketchfab_model(self, uid, normalize_size=False, target_size=1.0):
        """Download, validate, import, and optionally normalize one Sketchfab glTF archive."""
        if not isinstance(target_size, (int, float)) or isinstance(target_size, bool):
            raise ValueError("target_size must be a finite positive number")
        target_size = float(target_size)
        if not math.isfinite(target_size) or target_size <= 0:
            raise ValueError("target_size must be a finite positive number")
        api_key = self.get_sketchfab_api_key()
        if not api_key:
            raise ValueError("Sketchfab API key is not configured")

        headers = {"Authorization": f"Token {api_key}"}
        temp_dir = tempfile.mkdtemp(prefix="blender_mcp_sketchfab_")
        try:
            metadata = get_json(f"https://api.sketchfab.com/v3/models/{uid}", headers=headers)
            download = get_json(f"https://api.sketchfab.com/v3/models/{uid}/download", headers=headers)
            gltf = download.get("gltf") if isinstance(download, dict) else None
            download_url = gltf.get("url") if isinstance(gltf, dict) else None
            if not download_url:
                raise ValueError("No glTF download is available for this model")

            archive_path = os.path.join(temp_dir, "model.zip")
            download_file(download_url, archive_path, max_bytes=_MAX_ARCHIVE_BYTES)
            with zipfile.ZipFile(archive_path, "r") as archive:
                _validate_archive(archive)
                archive.extractall(temp_dir)

            candidates = sorted(
                os.path.join(root, filename)
                for root, _directories, files in os.walk(temp_dir)
                for filename in files
                if filename.lower().endswith((".gltf", ".glb"))
            )
            if not candidates:
                raise ValueError("No glTF file was found in the downloaded archive")
            main_file = next((path for path in candidates if path.lower().endswith(".gltf")), candidates[0])

            before_ids = {obj.session_uid for obj in bpy.data.objects}
            result = bpy.ops.import_scene.gltf(filepath=main_file)
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
        except (requests.exceptions.Timeout, json.JSONDecodeError, ValueError, RuntimeError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": f"Failed to import model: {exc!s}"}
        finally:
            with suppress(Exception):
                shutil.rmtree(temp_dir)

    # endregion

    # endregion
