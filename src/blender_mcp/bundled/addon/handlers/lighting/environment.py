# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Non-destructive World background, HDRI, and procedural-sky handlers."""

import math
import os

import bpy
import mathutils

from ._shared import (
    ensure_collection,
    finite_number,
    finite_vector,
    managed_node,
    node_tree_snapshot,
    plain,
    replace_input_link,
    restore_input_links,
    scene_by_name,
    world_for_edit,
)

MAX_ENVIRONMENT_BYTES = 1024 * 1024 * 1024


class _ManagedWorldEdit:
    """Track a managed world-node edit so a handler can restore it on failure."""

    def __init__(self, scene, world, created_world) -> None:
        self.scene = scene
        self.world = world
        self.previous_world = scene.world
        self.previous_use_nodes = bool(world.use_nodes)
        self.created_world = created_world
        self.created_nodes = []
        self.changed_values = []
        self.replaced_inputs = []

    def set_value(self, owner, field, value) -> None:
        """Assign one property while retaining its previous value."""
        self.changed_values.append((owner, field, getattr(owner, field)))
        setattr(owner, field, value)

    def set_socket(self, socket, value) -> None:
        """Assign one socket default while retaining its previous value."""
        previous = socket.default_value
        if hasattr(previous, "copy"):
            previous = previous.copy()
        self.changed_values.append((socket, "default_value", previous))
        socket.default_value = value

    def node(self, node_type, role):
        """Resolve a managed node and remember whether this edit created it."""
        node, created = managed_node(self.world.node_tree.nodes, node_type, role)
        if created:
            self.created_nodes.append(node)
        return node

    def link(self, from_socket, to_socket) -> None:
        """Replace one input link and retain its previous endpoints."""
        old = replace_input_link(self.world.node_tree, from_socket, to_socket)
        self.replaced_inputs.append((to_socket, old))

    def clear_input(self, to_socket) -> None:
        """Disconnect one managed input while retaining prior links."""
        old = [(link.from_socket, link.to_socket) for link in list(to_socket.links)]
        for link in list(to_socket.links):
            self.world.node_tree.links.remove(link)
        self.replaced_inputs.append((to_socket, old))

    def rollback(self) -> None:
        """Best-effort restore values, links, node existence, and scene assignment."""
        for owner, field, value in reversed(self.changed_values):
            try:
                setattr(owner, field, value)
            except Exception:
                pass
        for socket, links in reversed(self.replaced_inputs):
            try:
                restore_input_links(self.world.node_tree, socket, links)
            except Exception:
                pass
        for node in reversed(self.created_nodes):
            try:
                self.world.node_tree.nodes.remove(node)
            except Exception:
                pass
        self.world.use_nodes = self.previous_use_nodes
        self.scene.world = self.previous_world
        if self.created_world and self.world.users == 0:
            bpy.data.worlds.remove(self.world)


def _begin_world_edit(scene, world_name, create_world):
    """Resolve a world and initialize managed mutation bookkeeping."""
    previous_world = scene.world
    world_existed = world_name is None or bpy.data.worlds.get(world_name) is not None
    world = world_for_edit(scene, world_name, create_world)
    edit = _ManagedWorldEdit(scene, world, created_world=not world_existed)
    edit.previous_world = previous_world
    world.use_nodes = True
    if edit.created_world:
        # Blender's default Background/Output pair, not user nodes: left in, it sits beside the managed pair.
        world.node_tree.nodes.clear()
    return world, edit


def _managed_background(edit):
    """Resolve the managed World Output and Background nodes."""
    background = edit.node("ShaderNodeBackground", "background")
    output = edit.node("ShaderNodeOutputWorld", "world_output")
    edit.set_value(output, "is_active_output", True)
    return background, output


def _environment_result(scene, world, source, created_nodes, changed_objects=None, warnings=None):
    """Build a consistent environment-mutation result."""
    world_scenes = sorted(candidate.name for candidate in bpy.data.scenes if candidate.world == world)
    result_warnings = list(warnings or [])
    if len(world_scenes) > 1:
        result_warnings.append(
            f"World '{world.name}' is shared by {len(world_scenes)} scenes; "
            "its managed lighting graph changed for all of them."
        )
    return {
        "scene": scene.name,
        "world": world.name,
        "source": source,
        "managed_nodes_created": [node.name for node in created_nodes],
        "world_graph": node_tree_snapshot(world.node_tree),
        "transparent_film": bool(scene.render.film_transparent),
        "world_scene_users": world_scenes,
        "warnings": result_warnings,
        "changed_objects": changed_objects or [],
        "changed_resources": [world.name],
    }


def _validate_color(color):
    """Validate a normalized RGB triplet."""
    result = finite_vector(color, "color")
    if any(channel < 0 or channel > 1 for channel in result):
        raise ValueError("color channels must be in [0, 1]")
    return result


def _validate_sky_settings(settings):
    """Validate sky controls and translate public dust density to Blender RNA."""
    settings = dict(settings or {})
    allowed = {
        "sky_type",
        "sun_elevation",
        "sun_rotation",
        "altitude",
        "air_density",
        "dust_density",
        "ozone_density",
        "sun_size",
        "sun_intensity",
        "sun_disc",
        "background_strength",
    }
    unknown = set(settings) - allowed
    if unknown:
        raise ValueError(f"Unsupported sky fields: {sorted(unknown)}")
    settings.setdefault("sky_type", "MULTIPLE_SCATTERING")
    sky_types = {"MULTIPLE_SCATTERING", "SINGLE_SCATTERING", "PREETHAM", "HOSEK_WILKIE"}
    if settings["sky_type"] not in sky_types:
        raise ValueError(f"sky_type must be one of {sorted(sky_types)}")
    for field in (
        "sun_elevation",
        "sun_rotation",
        "altitude",
        "air_density",
        "dust_density",
        "ozone_density",
        "sun_size",
        "sun_intensity",
        "background_strength",
    ):
        if field in settings:
            settings[field] = finite_number(settings[field], field)
    if "sun_elevation" in settings and not -math.pi / 2 <= settings["sun_elevation"] <= math.pi / 2:
        raise ValueError("sun_elevation must be in [-pi/2, pi/2]")
    for field in ("altitude", "air_density", "dust_density", "ozone_density", "sun_intensity", "background_strength"):
        if field in settings and settings[field] < 0:
            raise ValueError(f"{field} must be non-negative")
    if "altitude" in settings and settings["altitude"] > 100_000:
        raise ValueError("altitude must be in [0, 100000]")
    if "sun_size" in settings and not 0 < settings["sun_size"] <= math.pi / 2:
        raise ValueError("sun_size must be in (0, pi/2]")
    if "sun_intensity" in settings and settings["sun_intensity"] > 1_000:
        raise ValueError("sun_intensity must be in [0, 1000]")
    return settings


class EnvironmentLightingHandlers:
    """Configure managed World illumination while preserving unrelated user-authored nodes."""

    def configure_world_background(
        self,
        scene_name,
        color=None,
        strength=None,
        transparent_film=None,
        world_name=None,
        create_world=False,
    ):
        """Create or patch a simple managed World background."""
        scene = scene_by_name(scene_name)
        if color is None and strength is None and transparent_film is None:
            raise ValueError("Provide color, strength, or transparent_film")
        color = _validate_color(color) if color is not None else None
        if strength is not None and finite_number(strength, "strength") < 0:
            raise ValueError("strength must be non-negative")
        if create_world and not world_name:
            raise ValueError("world_name is required when create_world is true")
        previous_film = bool(scene.render.film_transparent)
        world, edit = _begin_world_edit(scene, world_name, create_world)
        try:
            background, output = _managed_background(edit)
            if color is not None:
                edit.clear_input(background.inputs["Color"])
                edit.set_socket(background.inputs["Color"], (*color, 1.0))
            if strength is not None:
                edit.set_socket(background.inputs["Strength"], float(strength))
            edit.link(background.outputs["Background"], output.inputs["Surface"])
            if transparent_film is not None:
                scene.render.film_transparent = bool(transparent_film)
        except Exception:
            scene.render.film_transparent = previous_film
            edit.rollback()
            raise
        return _environment_result(scene, world, "BACKGROUND", edit.created_nodes)

    def configure_hdri_environment(
        self,
        scene_name,
        image_path,
        strength=1.0,
        rotation=0.0,
        projection="EQUIRECTANGULAR",
        replacement_policy="REPLACE_MANAGED",
        world_name=None,
        create_world=False,
        transparent_film=None,
    ):
        """Configure a persistent managed HDRI world chain."""
        scene = scene_by_name(scene_name)
        if not isinstance(image_path, str) or not os.path.isabs(image_path):
            raise ValueError("image_path must be absolute")
        extension = os.path.splitext(image_path)[1].lower()
        if extension not in {".hdr", ".exr"}:
            raise ValueError("image_path must use .hdr or .exr")
        if not os.path.isfile(image_path):
            raise ValueError(f"Environment image not found: {image_path}")
        size = os.path.getsize(image_path)
        if size <= 0 or size > MAX_ENVIRONMENT_BYTES:
            raise ValueError(f"Environment image size must be in [1, {MAX_ENVIRONMENT_BYTES}] bytes")
        strength = finite_number(strength, "strength")
        rotation = finite_number(rotation, "rotation")
        if strength < 0:
            raise ValueError("strength must be non-negative")
        if projection not in {"EQUIRECTANGULAR", "MIRROR_BALL"}:
            raise ValueError("projection must be EQUIRECTANGULAR or MIRROR_BALL")
        if replacement_policy not in {"REPLACE_MANAGED", "ERROR_IF_MANAGED"}:
            raise ValueError("replacement_policy must be REPLACE_MANAGED or ERROR_IF_MANAGED")
        if create_world and not world_name:
            raise ValueError("world_name is required when create_world is true")
        previous_film = bool(scene.render.film_transparent)
        world, edit = _begin_world_edit(scene, world_name, create_world)
        if replacement_policy == "ERROR_IF_MANAGED" and any(
            node.get("mcp_lighting_owner") == "blender-mcp" and node.get("mcp_role") in {"environment_texture", "sky"}
            for node in world.node_tree.nodes
        ):
            edit.rollback()
            raise ValueError("A managed HDRI or sky source already exists; choose REPLACE_MANAGED to update it")
        image = None
        image_was_loaded = False
        try:
            loaded_images = {candidate.as_pointer() for candidate in bpy.data.images}
            image = bpy.data.images.load(image_path, check_existing=True)
            image_was_loaded = image.as_pointer() not in loaded_images
            texture_coordinates = edit.node("ShaderNodeTexCoord", "texture_coordinate")
            mapping = edit.node("ShaderNodeMapping", "environment_mapping")
            environment = edit.node("ShaderNodeTexEnvironment", "environment_texture")
            background, output = _managed_background(edit)
            edit.set_value(environment, "image", image)
            edit.set_value(environment, "projection", projection)
            edit.set_socket(mapping.inputs["Rotation"], (0.0, 0.0, rotation))
            edit.set_socket(background.inputs["Strength"], strength)
            edit.link(texture_coordinates.outputs["Generated"], mapping.inputs["Vector"])
            edit.link(mapping.outputs["Vector"], environment.inputs["Vector"])
            edit.link(environment.outputs["Color"], background.inputs["Color"])
            edit.link(background.outputs["Background"], output.inputs["Surface"])
            if transparent_film is not None:
                scene.render.film_transparent = bool(transparent_film)
        except Exception:
            scene.render.film_transparent = previous_film
            edit.rollback()
            if image_was_loaded and image is not None and image.users == 0:
                bpy.data.images.remove(image)
            raise
        result = _environment_result(scene, world, "HDRI", edit.created_nodes)
        result.update(
            {
                "image": image.name,
                "image_path": bpy.path.abspath(image.filepath),
                "image_size_bytes": size,
                "color_space": image.colorspace_settings.name,
                "projection": environment.projection,
                "rotation_radians": rotation,
                "strength": float(background.inputs["Strength"].default_value),
            }
        )
        return result

    def configure_procedural_sky(
        self,
        scene_name,
        settings,
        target_engine="BOTH",
        sync_sun=False,
        sun_name=None,
        sun_collection_name="Lighting",
        sun_energy=1.0,
        world_name=None,
        create_world=False,
    ):
        """Configure a managed physical sky and optional synchronized Sun object."""
        scene = scene_by_name(scene_name)
        settings = _validate_sky_settings(settings)
        if target_engine not in {"BOTH", "CYCLES", "EEVEE"}:
            raise ValueError("target_engine must be BOTH, CYCLES, or EEVEE")
        if sync_sun and not sun_name:
            raise ValueError("sun_name is required when sync_sun is true")
        sun_energy = finite_number(sun_energy, "sun_energy")
        if sun_energy < 0:
            raise ValueError("sun_energy must be non-negative")
        existing_sun = bpy.data.objects.get(sun_name) if sun_name else None
        if existing_sun is not None and (existing_sun.type != "LIGHT" or existing_sun.data.type != "SUN"):
            raise ValueError(f"Object '{sun_name}' exists but is not a Sun light")
        if existing_sun is not None and existing_sun.name not in scene.objects:
            raise ValueError(f"Sun '{sun_name}' is not linked to scene '{scene.name}'")
        world, edit = _begin_world_edit(scene, world_name, create_world)
        sun_created = False
        old_sun_matrix = existing_sun.matrix_world.copy() if existing_sun is not None else None
        old_sun_values = (existing_sun.data.energy, existing_sun.data.angle) if existing_sun is not None else None
        try:
            sky = edit.node("ShaderNodeTexSky", "sky")
            background, output = _managed_background(edit)
            sky_type = settings.get("sky_type", "MULTIPLE_SCATTERING")
            valid_sky_types = {item.identifier for item in sky.bl_rna.properties["sky_type"].enum_items}
            if sky_type not in valid_sky_types:
                raise ValueError(f"Sky model '{sky_type}' is unavailable; choose from {sorted(valid_sky_types)}")
            field_map = {"dust_density": "aerosol_density"}
            for public_field in (
                "sky_type",
                "sun_elevation",
                "sun_rotation",
                "altitude",
                "air_density",
                "dust_density",
                "ozone_density",
                "sun_size",
                "sun_intensity",
                "sun_disc",
            ):
                if public_field in settings:
                    rna_field = field_map.get(public_field, public_field)
                    if not hasattr(sky, rna_field):
                        raise ValueError(f"Running Blender does not support sky field '{public_field}'")
                    edit.set_value(sky, rna_field, settings[public_field])
            if "background_strength" in settings:
                edit.set_socket(background.inputs["Strength"], settings["background_strength"])
            edit.link(sky.outputs["Color"], background.inputs["Color"])
            edit.link(background.outputs["Background"], output.inputs["Surface"])
            sun = existing_sun
            if sync_sun:
                if sun is None:
                    collection = ensure_collection(scene, sun_collection_name)
                    data_name = f"{sun_name} Light"
                    if bpy.data.lights.get(data_name) is not None:
                        raise ValueError(f"Light datablock already exists: {data_name}")
                    data = bpy.data.lights.new(data_name, "SUN")
                    sun = bpy.data.objects.new(sun_name, data)
                    collection.objects.link(sun)
                    sun_created = True
                elevation = float(sky.sun_elevation)
                rotation = float(sky.sun_rotation)
                toward_sun = mathutils.Vector(
                    (
                        math.cos(elevation) * math.cos(rotation),
                        math.cos(elevation) * math.sin(rotation),
                        math.sin(elevation),
                    )
                )
                sun.matrix_world = mathutils.Matrix.LocRotScale(
                    sun.matrix_world.translation,
                    (-toward_sun).to_track_quat("-Z", "Y"),
                    sun.matrix_world.to_scale(),
                )
                sun.data.energy = sun_energy
                sun.data.angle = float(sky.sun_size)
        except Exception:
            if existing_sun is not None and old_sun_values is not None:
                existing_sun.matrix_world = old_sun_matrix
                existing_sun.data.energy, existing_sun.data.angle = old_sun_values
            elif sun_created and sun is not None:
                data = sun.data
                bpy.data.objects.remove(sun, do_unlink=True)
                if data.users == 0:
                    bpy.data.lights.remove(data)
            edit.rollback()
            raise
        warnings = []
        if target_engine in {"BOTH", "EEVEE"} and not sync_sun:
            warnings.append("The Sky Texture sun disc is Cycles-only; EEVEE has no synchronized Sun light.")
        if sync_sun and bool(getattr(sky, "sun_disc", False)):
            warnings.append(
                "Cycles may receive direct energy from both the sky sun disc and the synchronized Sun light."
            )
        result = _environment_result(
            scene,
            world,
            "PROCEDURAL_SKY",
            edit.created_nodes,
            changed_objects=[sun.name] if sync_sun and sun is not None else [],
            warnings=warnings,
        )
        result.update(
            {
                "target_engine": target_engine,
                "sky": {
                    field: plain(getattr(sky, {"dust_density": "aerosol_density"}.get(field, field)))
                    for field in settings
                    if field != "background_strength"
                },
                "background_strength": float(background.inputs["Strength"].default_value),
                "synchronized_sun": sun.name if sync_sun and sun is not None else None,
            }
        )
        if sync_sun and sun is not None:
            result["changed_resources"].append(sun.data.name)
        return result
