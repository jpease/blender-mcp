# pyright: reportUnhashable=false
"""Image datablock inspection, loading, interpretation, and saving handlers."""

import os

from pathlib import Path

import bpy

from ._shared import SUPPORTED_IMAGE_EXTENSIONS, image_path_missing, material_by_name, required_name

DATA_SEMANTICS = {"NORMAL", "ROUGHNESS", "METALLIC", "AO", "HEIGHT", "DATA"}


def _image_materials(image):
    materials = []
    for material in bpy.data.materials:
        if (
            material.use_nodes
            and material.node_tree
            and any(node.bl_idname == "ShaderNodeTexImage" and node.image == image for node in material.node_tree.nodes)
        ):
            materials.append(material.name)
    return sorted(materials)


def _image_snapshot(image):
    width, height = (int(value) for value in image.size)
    channels = int(image.channels)
    depth = int(image.depth)
    return {
        "name": image.name,
        "dimensions": [width, height],
        "channels": channels,
        "bit_depth": depth,
        "source": image.source,
        "colorspace": image.colorspace_settings.name,
        "alpha_mode": image.alpha_mode,
        "filepath": image.filepath,
        "packed": image.packed_file is not None,
        "dirty": bool(image.is_dirty),
        "tiles": [{"number": tile.number, "label": tile.label} for tile in getattr(image, "tiles", [])],
        "users": image.users,
        "materials": _image_materials(image),
        "missing_file": image_path_missing(image),
        "estimated_memory_bytes": width * height * max(channels, 1) * max(depth // 8, 1),
    }


class TextureImageHandlers:
    """Inspect and safely mutate Blender image datablocks and explicit files."""

    def list_texture_images(self, material_name=None, include_unused=True, limit=50, offset=0):
        if material_name:
            material = material_by_name(material_name)
            images = (
                {
                    node.image
                    for node in material.node_tree.nodes
                    if node.bl_idname == "ShaderNodeTexImage" and node.image
                }
                if material.use_nodes and material.node_tree
                else set()
            )
        else:
            images = set(bpy.data.images)
        if not include_unused:
            images = {image for image in images if image.users > 0}
        ordered = sorted(images, key=lambda image: image.name.casefold())
        page = ordered[int(offset) : int(offset) + int(limit)]
        next_offset = int(offset) + len(page)
        return {
            "images": [_image_snapshot(image) for image in page],
            "total": len(ordered),
            "offset": int(offset),
            "limit": int(limit),
            "truncated": next_offset < len(ordered),
            "next_offset": next_offset if next_offset < len(ordered) else None,
        }

    def load_texture_image(self, path, name=None, check_existing=True, max_bytes=536_870_912):
        resolved = os.path.realpath(path)
        if not os.path.isfile(resolved):
            raise ValueError(f"Image file does not exist: {path}")
        suffix = Path(resolved).suffix.lower()
        if suffix not in SUPPORTED_IMAGE_EXTENSIONS:
            raise ValueError(f"Unsupported image extension '{suffix}'; supported={sorted(SUPPORTED_IMAGE_EXTENSIONS)}")
        size = os.path.getsize(resolved)
        if size > int(max_bytes):
            raise ValueError(f"Image file is {size} bytes, exceeding max_bytes={max_bytes}")
        previous = {image.as_pointer(): image for image in bpy.data.images}
        image = bpy.data.images.load(resolved, check_existing=bool(check_existing))
        created = image.as_pointer() not in previous
        if name:
            requested_name = required_name(name, "name")
            collision = bpy.data.images.get(requested_name)
            if collision is not None and collision != image:
                if created and image.users == 0:
                    bpy.data.images.remove(image)
                raise ValueError(f"Image name already exists: {requested_name}")
            image.name = requested_name
        return {
            "image": _image_snapshot(image),
            "loaded": created,
            "reused": not created,
            "file_size_bytes": size,
            "changed_resources": [image.name] if created else [],
        }

    def configure_texture_image(self, image_name, semantic=None, colorspace=None, alpha_mode=None):
        image = bpy.data.images.get(required_name(image_name, "image_name"))
        if image is None:
            raise ValueError(f"Image not found: {image_name}")
        before = {"colorspace": image.colorspace_settings.name, "alpha_mode": image.alpha_mode}
        requested_colorspace = colorspace
        if requested_colorspace is None and semantic:
            requested_colorspace = "Non-Color" if str(semantic).upper() in DATA_SEMANTICS else "sRGB"
        if requested_colorspace:
            try:
                image.colorspace_settings.name = requested_colorspace
            except TypeError as exc:
                raise ValueError(
                    f"Colorspace '{requested_colorspace}' is unavailable in the active OCIO configuration"
                ) from exc
        if alpha_mode:
            image.alpha_mode = str(alpha_mode).upper()
        after = {"colorspace": image.colorspace_settings.name, "alpha_mode": image.alpha_mode}
        changed = before != after
        return {
            "image": image.name,
            "before": before,
            "after": after,
            "changed_resources": [image.name] if changed else [],
        }

    def save_texture_image(
        self, image_name, output_path, file_format=None, color_mode=None, color_depth=None, overwrite=False
    ):
        image = bpy.data.images.get(required_name(image_name, "image_name"))
        if image is None:
            raise ValueError(f"Image not found: {image_name}")
        destination = os.path.realpath(os.path.expanduser(output_path))
        parent = os.path.dirname(destination)
        if not os.path.isdir(parent):
            # The caller's own text, not the resolved path, which can expose Blender's working directory.
            raise ValueError(f"Output directory does not exist: {os.path.dirname(output_path) or '.'}")
        if os.path.exists(destination) and not overwrite:
            raise ValueError(f"Output file already exists: {destination}")
        settings = bpy.context.scene.render.image_settings
        old = (image.filepath_raw, image.file_format, settings.color_mode, settings.color_depth)
        try:
            image.filepath_raw = destination
            if file_format:
                image.file_format = file_format
            if color_mode:
                settings.color_mode = color_mode
            if color_depth:
                settings.color_depth = str(color_depth)
            image.save()
            if not os.path.isfile(destination):
                raise RuntimeError("Blender reported success but no output file was created")
        except Exception:
            image.filepath_raw, image.file_format, settings.color_mode, settings.color_depth = old
            raise
        return {
            "image": image.name,
            "output_path": destination,
            "file_size_bytes": os.path.getsize(destination),
            "format": image.file_format,
            "changed_resources": [image.name],
        }
