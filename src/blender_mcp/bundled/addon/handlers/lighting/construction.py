# pyright: reportArgumentType=false, reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Light creation, typed configuration, parent-safe aiming, and linking handlers."""

import math

import bpy
import mathutils

from ._shared import (
    LIGHT_TYPES,
    collection_in_scene,
    collection_is_in_tree,
    ensure_collection,
    evaluated_bounds_point,
    evaluated_object_bounds,
    finite_number,
    finite_vector,
    light_linking_snapshot,
    light_object,
    light_settings_snapshot,
    object_in_scene,
    patch_properties,
    required_name,
    rounded,
    scene_by_name,
    transform_snapshot,
    validate_light_patch,
)

LIGHTING_OWNER = "blender-mcp"

# Placement distance (multiplier on the target's evaluated bounding-sphere radius) for each rig
# role. Kept mood-independent: apparent softbox size (STUDIO_LIGHTING_MOODS[...]["size_factor"])
# is what actually drives hard-vs-soft shadow character at a given distance.
STUDIO_LIGHTING_DISTANCE_FACTORS = {"key": 3.5, "fill": 4.5, "rim": 3.0}

_DEGENERATE_BOUNDS_EPSILON = 1e-6
_ZERO_VECTOR_EPSILON = 1e-9

# Photographic key/fill/rim presets. Angles are degrees measured from the horizontal projection of
# the camera-to-subject axis ("forward"), rotated around world Z; rim's azimuth sits near the far
# side (~180 degrees) so it grazes the subject's silhouette instead of aiming straight at the lens.
# size_factor scales the AREA light's SQUARE `size` off the same bounding-sphere radius, so a light's
# apparent angular size (softness) is size_factor/distance_factor - independent of scene scale.
STUDIO_LIGHTING_MOODS = {
    "SOFT": {
        "default_key_ratio": 2.0,
        "rim_ratio": 1.5,
        "irradiance_constant": 90.0,
        "key": {"elevation": 40.0, "azimuth": 35.0, "size_factor": 3.5},
        "fill": {"elevation": 20.0, "azimuth": -45.0, "size_factor": 4.0},
        "rim": {"elevation": 55.0, "azimuth": 160.0, "size_factor": 1.8},
    },
    "HIGH_CONTRAST": {
        "default_key_ratio": 8.0,
        "rim_ratio": 1.0,
        "irradiance_constant": 140.0,
        "key": {"elevation": 25.0, "azimuth": 45.0, "size_factor": 1.0},
        "fill": {"elevation": 15.0, "azimuth": -55.0, "size_factor": 1.5},
        "rim": {"elevation": 45.0, "azimuth": 165.0, "size_factor": 0.8},
    },
    "BEAUTY": {
        "default_key_ratio": 3.0,
        "rim_ratio": 2.5,
        "irradiance_constant": 70.0,
        "key": {"elevation": 50.0, "azimuth": 20.0, "size_factor": 4.5, "temperature": 3200.0},
        "fill": {"elevation": 35.0, "azimuth": -25.0, "size_factor": 3.5, "temperature": 3600.0},
        "rim": {"elevation": 60.0, "azimuth": 160.0, "size_factor": 1.5, "temperature": 5600.0},
    },
}


def _validate_target_bone(target, bone_name):
    """Resolve a pose-bone target and return its evaluated world position."""
    if target.type != "ARMATURE" or target.pose is None:
        raise ValueError(f"Object '{target.name}' is not an armature with pose bones")
    bone = target.pose.bones.get(required_name(bone_name, "target_bone_name"))
    if bone is None:
        raise ValueError(f"Pose bone not found: {bone_name}")
    return target.matrix_world @ bone.matrix.translation


def _world_aim_matrix(obj, target):
    """Build a matrix whose local -Z axis aims at a world point while preserving location and scale."""
    location, _rotation, scale = obj.matrix_world.decompose()
    direction = target - location
    if direction.length_squared <= 1e-16:
        raise ValueError("Light and aim target cannot occupy the same world position")
    rotation = direction.to_track_quat("-Z", "Y")
    return mathutils.Matrix.LocRotScale(location, rotation, scale), direction.normalized()


def _existing_or_new_helper(scene, collection_name, helper_name, location):
    """Resolve a tagged lighting target or create one in the explicit helpers collection."""
    helper = bpy.data.objects.get(helper_name)
    created = False
    old_matrix = None
    if helper is not None:
        if helper.type != "EMPTY" or helper.get("mcp_lighting_role") != "aim_target":
            raise ValueError(f"Object '{helper_name}' exists but is not an MCP lighting aim target")
        if helper.name not in scene.objects:
            raise ValueError(f"Aim helper '{helper_name}' is not linked to scene '{scene.name}'")
        old_matrix = helper.matrix_world.copy()
    else:
        collection = ensure_collection(scene, collection_name)
        helper = bpy.data.objects.new(helper_name, None)
        helper.empty_display_type = "SPHERE"
        helper.empty_display_size = 0.2
        helper["mcp_lighting_owner"] = LIGHTING_OWNER
        helper["mcp_lighting_role"] = "aim_target"
        collection.objects.link(helper)
        created = True
    helper.matrix_world.translation = location
    return helper, created, old_matrix


class LightConstructionHandlers:
    """Create and safely mutate ordinary Blender light objects and their light-link assignments."""

    def create_light(
        self,
        scene_name,
        collection_name,
        name,
        light_type,
        location,
        rotation_euler=(0.0, 0.0, 0.0),
        settings=None,
    ):
        """Create one validated, unselected light through Blender's data API."""
        scene = scene_by_name(scene_name)
        required_name(name, "name")
        required_name(collection_name, "collection_name")
        if light_type not in LIGHT_TYPES:
            raise ValueError(f"light_type must be one of {sorted(LIGHT_TYPES)}")
        if bpy.data.objects.get(name) is not None:
            raise ValueError(f"Object already exists: {name}")
        data_name = f"{name} Light"
        if bpy.data.lights.get(data_name) is not None:
            raise ValueError(f"Light datablock already exists: {data_name}")
        world_location = finite_vector(location, "location")
        world_rotation = finite_vector(rotation_euler, "rotation_euler")
        requested = {
            "energy": 1000.0,
            "exposure": 0.0,
            "normalize": True,
            "use_shadow": True,
            **(settings or {}),
        }
        allowed = validate_light_patch(light_type, requested)
        collection = bpy.data.collections.get(collection_name)
        collection_existed = collection is not None
        collection_was_in_scene = collection_existed and collection_is_in_tree(scene.collection, collection)
        data = None
        obj = None
        try:
            collection = ensure_collection(scene, collection_name)
            data = bpy.data.lights.new(data_name, light_type)
            obj = bpy.data.objects.new(name, data)
            collection.objects.link(obj)
            patch_properties(data, requested, allowed)
            obj.rotation_mode = "XYZ"
            obj.location = world_location
            obj.rotation_euler = world_rotation
        except Exception:
            if obj is not None:
                bpy.data.objects.remove(obj, do_unlink=True)
            if data is not None and data.users == 0:
                bpy.data.lights.remove(data)
            if collection is not None and not collection_was_in_scene:
                linked = scene.collection.children.get(collection.name)
                if linked == collection:
                    scene.collection.children.unlink(collection)
            if collection is not None and not collection_existed and collection.users == 0:
                bpy.data.collections.remove(collection)
            raise
        return {
            "scene": scene.name,
            "collection": collection.name,
            "object": obj.name,
            "light_data": data.name,
            "light_type": data.type,
            "transform": transform_snapshot(obj),
            "settings": light_settings_snapshot(data),
            "scene_unit_scale": float(scene.unit_settings.scale_length),
            "changed_objects": [obj.name],
            "changed_resources": [data.name],
        }

    def configure_light(self, light_name, patch):
        """Atomically patch allowlisted fields on one existing light datablock."""
        obj = light_object(light_name)
        patch = dict(patch or {})
        if not patch:
            raise ValueError("Provide at least one light setting to change")
        allowed = validate_light_patch(obj.data.type, patch)
        old, new = patch_properties(obj.data, patch, allowed)
        affected = sorted(
            candidate.name for candidate in bpy.data.objects if candidate.type == "LIGHT" and candidate.data == obj.data
        )
        warnings = []
        if len(affected) > 1:
            warnings.append(
                f"Light datablock '{obj.data.name}' has {len(affected)} object users; all listed objects changed."
            )
        return {
            "object": obj.name,
            "light_data": obj.data.name,
            "light_type": obj.data.type,
            "old": old,
            "new": new,
            "effective": light_settings_snapshot(obj.data),
            "data_users": affected,
            "warnings": warnings,
            "changed_objects": affected,
            "changed_resources": [obj.data.name],
        }

    def aim_light(
        self,
        scene_name,
        light_name,
        target_point=None,
        target_object_name=None,
        target_bone_name=None,
        bounds_position="CENTER",
        method="STATIC_ROTATION",
        constraint_name="MCP Light Aim",
        helper_name=None,
        helper_collection_name="Lighting Helpers",
    ):
        """Aim local -Z at a validated world target using a rotation or live constraint."""
        scene = scene_by_name(scene_name)
        light = light_object(light_name, scene=scene)
        if (target_point is None) == (target_object_name is None):
            raise ValueError("Supply exactly one of target_point or target_object_name")
        if bounds_position not in {"CENTER", "TOP", "BOTTOM"}:
            raise ValueError("bounds_position must be CENTER, TOP, or BOTTOM")
        if method not in {"STATIC_ROTATION", "TRACK_TO", "DAMPED_TRACK"}:
            raise ValueError("method must be STATIC_ROTATION, TRACK_TO, or DAMPED_TRACK")
        constraint = None
        if method != "STATIC_ROTATION":
            required_name(constraint_name, "constraint_name")
            if target_point is not None or bounds_position != "CENTER":
                required_name(helper_name, "helper_name")
                required_name(helper_collection_name, "helper_collection_name")
            constraint = light.constraints.get(constraint_name)
            if constraint is not None and constraint.type != method:
                raise ValueError(
                    f"Constraint '{constraint_name}' exists with type {constraint.type}; choose another name"
                )
        target_object = object_in_scene(scene, target_object_name) if target_object_name else None
        if target_bone_name:
            if target_object is None:
                raise ValueError("target_bone_name requires target_object_name")
            world_target = _validate_target_bone(target_object, target_bone_name)
        elif target_point is not None:
            world_target = mathutils.Vector(finite_vector(target_point, "target_point"))
        elif bounds_position != "CENTER":
            world_target = evaluated_bounds_point(target_object, bounds_position)
        else:
            world_target = target_object.matrix_world.translation.copy()
        aimed_matrix, direction = _world_aim_matrix(light, world_target)
        if method == "STATIC_ROTATION":
            light.matrix_world = aimed_matrix
            return {
                "light": light.name,
                "method": method,
                "target": rounded(world_target),
                "world_direction": rounded(direction),
                "constraint": None,
                "transform": transform_snapshot(light),
                "changed_objects": [light.name],
            }
        live_target = target_object
        helper = None
        helper_created = False
        helper_old_matrix = None
        needs_helper = target_point is not None or bounds_position != "CENTER"
        if needs_helper:
            if not helper_name:
                raise ValueError("helper_name is required for a live point or evaluated-bounds target")
            helper, helper_created, helper_old_matrix = _existing_or_new_helper(
                scene, helper_collection_name, helper_name, world_target
            )
            live_target = helper
        constraint_created = False
        old_constraint = None
        try:
            if constraint is None:
                constraint = light.constraints.new(method)
                constraint.name = constraint_name
                constraint_created = True
            else:
                old_constraint = {
                    "target": constraint.target,
                    "subtarget": getattr(constraint, "subtarget", ""),
                    "track_axis": getattr(constraint, "track_axis", None),
                    "up_axis": getattr(constraint, "up_axis", None),
                }
            constraint.target = live_target
            if hasattr(constraint, "subtarget"):
                constraint.subtarget = target_bone_name if live_target is target_object and target_bone_name else ""
            constraint.track_axis = "TRACK_NEGATIVE_Z"
            if method == "TRACK_TO":
                constraint.up_axis = "UP_Y"
        except Exception:
            if constraint_created and constraint is not None:
                light.constraints.remove(constraint)
            elif old_constraint is not None:
                for field, value in old_constraint.items():
                    if value is not None:
                        setattr(constraint, field, value)
            if helper_created and helper is not None:
                bpy.data.objects.remove(helper, do_unlink=True)
            elif helper is not None and helper_old_matrix is not None:
                helper.matrix_world = helper_old_matrix
            raise
        changed = [light.name]
        if helper is not None:
            changed.append(helper.name)
        return {
            "light": light.name,
            "method": method,
            "target": rounded(world_target),
            "world_direction": rounded(direction),
            "constraint": {
                "name": constraint.name,
                "type": constraint.type,
                "target": constraint.target.name,
                "subtarget": getattr(constraint, "subtarget", ""),
                "track_axis": constraint.track_axis,
                "up_axis": getattr(constraint, "up_axis", None),
            },
            "helper": helper.name if helper else None,
            "changed_objects": changed,
        }

    def configure_light_linking(
        self,
        scene_name,
        light_name,
        receiver_collection_name=None,
        blocker_collection_name=None,
        clear_receivers=False,
        clear_blockers=False,
    ):
        """Atomically assign explicit receiver and blocker collections to one light."""
        scene = scene_by_name(scene_name)
        light = light_object(light_name, scene=scene)
        linking = getattr(light, "light_linking", None)
        if linking is None:
            raise ValueError("Running Blender does not expose Object.light_linking")
        if receiver_collection_name is not None and clear_receivers:
            raise ValueError("Choose receiver_collection_name or clear_receivers, not both")
        if blocker_collection_name is not None and clear_blockers:
            raise ValueError("Choose blocker_collection_name or clear_blockers, not both")
        receiver = collection_in_scene(scene, receiver_collection_name) if receiver_collection_name else None
        blocker = collection_in_scene(scene, blocker_collection_name) if blocker_collection_name else None
        if receiver is None and blocker is None and not clear_receivers and not clear_blockers:
            raise ValueError("Request at least one receiver or blocker change")
        old_receiver = linking.receiver_collection
        old_blocker = linking.blocker_collection
        before = light_linking_snapshot(light)
        try:
            if receiver is not None or clear_receivers:
                linking.receiver_collection = receiver
            if blocker is not None or clear_blockers:
                linking.blocker_collection = blocker
        except Exception:
            linking.receiver_collection = old_receiver
            linking.blocker_collection = old_blocker
            raise
        after = light_linking_snapshot(light)
        changed = before != after
        return {
            "light": light.name,
            "before": before,
            "after": after,
            "engine_support": ["CYCLES", "EEVEE"],
            "warnings": ["Render matched engine previews because linked-shadow behavior can differ by engine."],
            "changed_objects": [light.name] if changed else [],
        }

    def create_studio_lighting(
        self,
        scene_name,
        target_object_name,
        camera_name,
        mood="SOFT",
        key_ratio=None,
        rig_name=None,
        collection_name="Studio Lighting",
    ):
        """
        Build a photographically-sound key/fill/rim AREA-light rig sized to a target's bounds.

        Orchestrates create_light + aim_light for each role instead of duplicating their validation
        or mutation logic; placement, softbox size, and energy all scale off the target's evaluated
        bounding-sphere radius so the rig is proportionate regardless of scene scale.
        """
        scene = scene_by_name(scene_name)
        target = object_in_scene(scene, target_object_name)
        camera = object_in_scene(scene, camera_name)
        if camera.type != "CAMERA":
            raise ValueError(f"Object '{camera_name}' is not a camera")
        if mood not in STUDIO_LIGHTING_MOODS:
            raise ValueError(f"mood must be one of {sorted(STUDIO_LIGHTING_MOODS)}")
        preset = STUDIO_LIGHTING_MOODS[mood]
        if key_ratio is None:
            key_ratio = preset["default_key_ratio"]
        else:
            key_ratio = finite_number(key_ratio, "key_ratio")
            if key_ratio <= 0:
                raise ValueError("key_ratio must be positive")
        rig_name = required_name(rig_name or target_object_name, "rig_name")

        names = {role: f"{rig_name} {role.capitalize()}" for role in ("key", "fill", "rim")}
        for obj_name in names.values():
            if bpy.data.objects.get(obj_name) is not None:
                raise ValueError(f"Object already exists: {obj_name}")
            data_name = f"{obj_name} Light"
            if bpy.data.lights.get(data_name) is not None:
                raise ValueError(f"Light datablock already exists: {data_name}")

        minimum, maximum = evaluated_object_bounds(target)
        center = (minimum + maximum) * 0.5
        bbox_radius = (maximum - minimum).length / 2.0
        if bbox_radius <= _DEGENERATE_BOUNDS_EPSILON:
            raise ValueError(f"Object '{target_object_name}' has degenerate evaluated bounds")

        camera_offset = camera.matrix_world.translation - center
        forward = mathutils.Vector((camera_offset.x, camera_offset.y, 0.0))
        if forward.length_squared <= _ZERO_VECTOR_EPSILON:
            forward = mathutils.Vector((0.0, -1.0, 0.0))
        forward.normalize()

        collection = bpy.data.collections.get(collection_name)
        collection_existed = collection is not None
        collection_was_in_scene = collection_existed and collection_is_in_tree(scene.collection, collection)

        key_distance = bbox_radius * STUDIO_LIGHTING_DISTANCE_FACTORS["key"]
        key_energy = preset["irradiance_constant"] * key_distance**2
        role_energy = {"key": key_energy, "fill": key_energy / key_ratio, "rim": key_energy / preset["rim_ratio"]}

        created_roles = []
        lights = []
        try:
            for role in ("key", "fill", "rim"):
                role_preset = preset[role]
                distance = bbox_radius * STUDIO_LIGHTING_DISTANCE_FACTORS[role]
                elevation = math.radians(role_preset["elevation"])
                azimuth = math.radians(role_preset["azimuth"])
                direction = forward.copy()
                direction.rotate(mathutils.Quaternion((0.0, 0.0, 1.0), azimuth))
                location = (
                    center
                    + direction * (distance * math.cos(elevation))
                    + mathutils.Vector((0.0, 0.0, distance * math.sin(elevation)))
                )
                settings = {
                    "energy": role_energy[role],
                    "shape": "SQUARE",
                    "size": bbox_radius * role_preset["size_factor"],
                }
                if "temperature" in role_preset:
                    settings["use_temperature"] = True
                    settings["temperature"] = role_preset["temperature"]
                created = self.create_light(
                    scene.name,
                    collection_name,
                    names[role],
                    "AREA",
                    tuple(location),
                    settings=settings,
                )
                created_roles.append(role)
                self.aim_light(scene.name, names[role], target_point=tuple(center))
                lights.append(
                    {
                        "role": role,
                        "object": created["object"],
                        "light_data": created["light_data"],
                        "energy": created["settings"]["energy"],
                        "transform": transform_snapshot(bpy.data.objects[names[role]]),
                    }
                )
        except Exception:
            for role in created_roles:
                obj = bpy.data.objects.get(names[role])
                if obj is None:
                    continue
                data = obj.data
                bpy.data.objects.remove(obj, do_unlink=True)
                if data is not None and data.users == 0:
                    bpy.data.lights.remove(data)
            collection = bpy.data.collections.get(collection_name)
            if collection is not None and not collection_was_in_scene:
                linked = scene.collection.children.get(collection.name)
                if linked == collection:
                    scene.collection.children.unlink(collection)
            if collection is not None and not collection_existed and collection.users == 0:
                bpy.data.collections.remove(collection)
            raise

        return {
            "scene": scene.name,
            "rig_name": rig_name,
            "target_object": target.name,
            "camera": camera.name,
            "mood": mood,
            "key_ratio": key_ratio,
            "collection": collection_name,
            "lights": lights,
            "changed_objects": [entry["object"] for entry in lights],
            "changed_resources": [entry["light_data"] for entry in lights],
        }
