# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Reusable camera rig builders (orbit, dolly, crane, path) and rig-level transform/duplication utilities."""

import math
import uuid

import bpy
import mathutils

from ._shared import (
    _CAMERA_OPTICS,
    _MAX_FRAME,
    _MIN_FRAME,
    _RIG_SCHEMA_VERSION,
    _add_constraint,
    _bounded_int,
    _camera,
    _constraint_info,
    _ensure_collection,
    _finite_number,
    _matrix_close,
    _new_empty,
    _object,
    _plain,
    _positive,
    _required_name,
    _scene,
    _tag,
    _transform_info,
    _update_view_layer,
    _vector,
)

_OPTICS_COPY_FIELDS = _CAMERA_OPTICS - {"panorama_type"}
_MAX_RIG_MEMBERS = 2_000


def _new_camera_object(collection, name, lens, rig_id=None, role="camera"):
    data = bpy.data.cameras.new(f"{name} Data")
    data.lens = _positive(lens, "lens")
    obj = bpy.data.objects.new(name, data)
    collection.objects.link(obj)
    if rig_id is not None:
        _tag(obj, rig_id, role)
        data["mcp_camera_rig_id"] = rig_id
        data["mcp_camera_role"] = "camera_data"
        data["mcp_camera_schema_version"] = _RIG_SCHEMA_VERSION
        data["mcp_camera_owner"] = "blender-mcp"
    return obj


def _parent_local(child, parent, location=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0)):
    child.parent = parent
    child.matrix_parent_inverse = mathutils.Matrix.Identity(4)
    child.location = location
    child.rotation_mode = "XYZ"
    child.rotation_euler = rotation


def _copy_action(owner, policy):
    animation = getattr(owner, "animation_data", None)
    if animation is None or animation.action is None:
        return None
    if policy == "NONE":
        animation.action = None
    elif policy == "COPY":
        animation.action = animation.action.copy()
    return getattr(animation.action, "name", None)


class _RigsMixin:
    """Provide rig-builder, rig-level transform-matching, and rig-duplication handlers."""

    def create_orbit_camera_rig(
        self,
        scene_name,
        collection_name,
        rig_name,
        pivot,
        radius,
        azimuth=0.0,
        elevation=0.0,
        roll=0.0,
        lens=50.0,
        target_height=0.0,
    ):
        scene = _scene(scene_name)
        _required_name(rig_name, "rig_name")
        pivot = _vector(pivot, "pivot")
        radius = _positive(radius, "radius")
        azimuth = _finite_number(azimuth, "azimuth")
        elevation = _finite_number(elevation, "elevation")
        roll = _finite_number(roll, "roll")
        target_height = _finite_number(target_height, "target_height")
        _positive(lens, "lens")
        collection = _ensure_collection(scene, collection_name)
        rig_id = str(uuid.uuid4())
        root = _new_empty(collection, f"{rig_name} Root", pivot, rig_id, "root", display_type="CIRCLE")
        root.rotation_euler.z = azimuth
        boom = _new_empty(collection, f"{rig_name} Boom", (0.0, 0.0, 0.0), rig_id, "boom", display_type="CUBE")
        horizontal = radius * math.cos(elevation)
        _parent_local(boom, root, (horizontal, 0.0, radius * math.sin(elevation)))
        camera = _new_camera_object(collection, f"{rig_name} Camera", lens, rig_id)
        _parent_local(camera, boom, rotation=(0.0, 0.0, roll))
        target = _new_empty(
            collection,
            f"{rig_name} Target",
            (0.0, 0.0, target_height),
            rig_id,
            "target",
            display_type="SPHERE",
        )
        _parent_local(target, root, (0.0, 0.0, target_height))
        constraint = _add_constraint(camera, target, name="MCP Orbit Aim", constraint_type="DAMPED_TRACK")
        root["mcp_camera_azimuth"] = azimuth
        boom["mcp_camera_radius"] = radius
        boom["mcp_camera_elevation"] = elevation
        return self._rig_result("ORBIT", rig_id, collection, root, camera, [root, boom, target, camera], [constraint])

    def create_dolly_camera_rig(
        self,
        scene_name,
        collection_name,
        rig_name,
        location,
        rail_direction=(0.0, 1.0, 0.0),
        yaw=0.0,
        camera_height=1.5,
        target_distance=10.0,
        lens=50.0,
        create_target=True,
    ):
        scene = _scene(scene_name)
        _required_name(rig_name, "rig_name")
        location = _vector(location, "location")
        rail = _vector(rail_direction, "rail_direction")
        if rail.length_squared <= 1e-16:
            raise ValueError("rail_direction must be non-zero")
        rail.normalize()
        camera_height = _positive(camera_height, "camera_height", allow_zero=True)
        target_distance = _positive(target_distance, "target_distance")
        _positive(lens, "lens")
        collection = _ensure_collection(scene, collection_name)
        rig_id = str(uuid.uuid4())
        root = _new_empty(collection, f"{rig_name} Root", location, rig_id, "root", display_type="ARROWS")
        root.rotation_euler.z = _finite_number(yaw, "yaw")
        root["mcp_camera_rail_direction"] = list(rail)
        control = _new_empty(
            collection, f"{rig_name} Camera Control", (0.0, 0.0, 0.0), rig_id, "camera_control", display_type="CUBE"
        )
        _parent_local(control, root, (0.0, 0.0, camera_height))
        camera = _new_camera_object(collection, f"{rig_name} Camera", lens, rig_id)
        _parent_local(camera, control)
        members = [root, control, camera]
        constraints = []
        if create_target:
            world_rail = mathutils.Matrix.Rotation(float(root.rotation_euler.z), 4, "Z") @ rail
            target = _new_empty(
                collection,
                f"{rig_name} Target",
                location + world_rail * target_distance + mathutils.Vector((0.0, 0.0, camera_height)),
                rig_id,
                "target",
                display_type="SPHERE",
            )
            constraints.append(_add_constraint(camera, target, name="MCP Dolly Aim", constraint_type="DAMPED_TRACK"))
            members.append(target)
        return self._rig_result("DOLLY", rig_id, collection, root, camera, members, constraints)

    def create_crane_camera_rig(
        self,
        scene_name,
        collection_name,
        rig_name,
        location,
        base_height=1.0,
        arm_length=5.0,
        elevation=0.0,
        pan=0.0,
        tilt=0.0,
        roll=0.0,
        lens=50.0,
        create_target=True,
    ):
        scene = _scene(scene_name)
        _required_name(rig_name, "rig_name")
        location = _vector(location, "location")
        base_height = _positive(base_height, "base_height", allow_zero=True)
        arm_length = _positive(arm_length, "arm_length")
        angles = {
            key: _finite_number(value, key)
            for key, value in {"elevation": elevation, "pan": pan, "tilt": tilt, "roll": roll}.items()
        }
        _positive(lens, "lens")
        collection = _ensure_collection(scene, collection_name)
        rig_id = str(uuid.uuid4())
        root = _new_empty(collection, f"{rig_name} Base", location, rig_id, "root", display_type="CIRCLE")
        root.rotation_euler.z = angles["pan"]
        pivot = _new_empty(collection, f"{rig_name} Arm Pivot", (0.0, 0.0, 0.0), rig_id, "arm_pivot")
        _parent_local(pivot, root, (0.0, 0.0, base_height), (0.0, -angles["elevation"], 0.0))
        boom = _new_empty(collection, f"{rig_name} Boom", (0.0, 0.0, 0.0), rig_id, "boom", display_type="SINGLE_ARROW")
        _parent_local(boom, pivot, (arm_length, 0.0, 0.0))
        boom["mcp_camera_arm_length"] = arm_length
        head = _new_empty(collection, f"{rig_name} Head", (0.0, 0.0, 0.0), rig_id, "head", display_type="CUBE")
        _parent_local(head, boom, rotation=(angles["tilt"], 0.0, angles["roll"]))
        camera = _new_camera_object(collection, f"{rig_name} Camera", lens, rig_id)
        _parent_local(camera, head)
        members = [root, pivot, boom, head, camera]
        constraints = []
        if create_target:
            target = _new_empty(
                collection,
                f"{rig_name} Target",
                location,
                rig_id,
                "target",
                display_type="SPHERE",
            )
            constraints.append(_add_constraint(camera, target, name="MCP Crane Aim", constraint_type="DAMPED_TRACK"))
            members.append(target)
        return self._rig_result("CRANE", rig_id, collection, root, camera, members, constraints)

    def create_camera_path_rig(
        self,
        scene_name,
        collection_name,
        rig_name,
        camera_name,
        curve_object_name=None,
        path_points=None,
        spline_type="BEZIER",
        forward_axis="TRACK_NEGATIVE_Z",
        up_axis="UP_Y",
        use_curve_follow=True,
        start_frame=None,
        end_frame=None,
        target_object_name=None,
    ):
        scene = _scene(scene_name)
        _required_name(rig_name, "rig_name")
        camera = _camera(camera_name, scene=scene)
        original_camera_world = camera.matrix_world.copy()
        if (curve_object_name is None) == (path_points is None):
            raise ValueError("Supply exactly one of curve_object_name or path_points")
        if (start_frame is None) != (end_frame is None):
            raise ValueError("start_frame and end_frame must be supplied together")
        animation_start = animation_end = None
        if start_frame is not None:
            assert end_frame is not None
            animation_start = _bounded_int(start_frame, "start_frame", _MIN_FRAME, _MAX_FRAME)
            animation_end = _bounded_int(end_frame, "end_frame", _MIN_FRAME, _MAX_FRAME)
            if animation_start >= animation_end:
                raise ValueError("start_frame must be less than end_frame")
        target = _object(target_object_name, scene=scene) if target_object_name else None
        rig_id = str(uuid.uuid4())
        if curve_object_name is not None:
            curve = _object(curve_object_name, scene=scene)
            if curve.type != "CURVE":
                raise ValueError(f"Path object '{curve.name}' is not a curve (type={curve.type})")
            created_curve = False
        else:
            assert path_points is not None
            validated_points = [_vector(point, f"path_points[{index}]") for index, point in enumerate(path_points)]
            if len(validated_points) < 2:
                raise ValueError("path_points must contain at least two points")
            spline_type = str(spline_type).upper()
            if spline_type not in {"BEZIER", "NURBS"}:
                raise ValueError("spline_type must be BEZIER or NURBS")
            collection = _ensure_collection(scene, collection_name)
            curve_data = bpy.data.curves.new(f"{rig_name} Path Data", type="CURVE")
            curve_data.dimensions = "3D"
            spline = curve_data.splines.new("BEZIER" if spline_type == "BEZIER" else "NURBS")
            if spline_type == "BEZIER":
                spline.bezier_points.add(len(validated_points) - 1)
                for point, coordinate in zip(spline.bezier_points, validated_points, strict=True):
                    point.co = coordinate
                    point.handle_left_type = "AUTO"
                    point.handle_right_type = "AUTO"
            else:
                spline.points.add(len(validated_points) - 1)
                for point, coordinate in zip(spline.points, validated_points, strict=True):
                    point.co = (*coordinate, 1.0)
                spline.order_u = min(4, len(validated_points))
                spline.use_endpoint_u = True
            curve = bpy.data.objects.new(f"{rig_name} Path", curve_data)
            collection.objects.link(curve)
            _tag(curve, rig_id, "path")
            created_curve = True
        if curve_object_name is not None:
            collection = _ensure_collection(scene, collection_name)
        root = _new_empty(collection, f"{rig_name} Root", (0.0, 0.0, 0.0), rig_id, "root", display_type="ARROWS")
        constraint = root.constraints.new(type="FOLLOW_PATH")
        constraint.name = "MCP Camera Path"
        constraint.target = curve
        constraint.forward_axis = forward_axis
        constraint.up_axis = up_axis
        constraint.use_curve_follow = bool(use_curve_follow)
        constraint.use_fixed_location = True
        if animation_start is not None:
            assert animation_end is not None
            original_offset = constraint.offset_factor
            constraint.offset_factor = 0.0
            constraint.keyframe_insert(data_path="offset_factor", frame=animation_start)
            constraint.offset_factor = 1.0
            constraint.keyframe_insert(data_path="offset_factor", frame=animation_end)
            constraint.offset_factor = original_offset
        constraints = [constraint]
        if target is not None:
            constraints.append(_add_constraint(camera, target, name="MCP Path Aim", constraint_type="DAMPED_TRACK"))
        _update_view_layer()
        camera.parent = root
        camera.matrix_parent_inverse = root.matrix_world.inverted()
        camera.matrix_world = original_camera_world
        _tag(camera, rig_id, "camera")
        members = [root, camera]
        if created_curve:
            members.append(curve)
        result = self._rig_result("PATH", rig_id, collection, root, camera, members, constraints)
        result["path"] = curve.name
        result["path_created"] = created_curve
        result["animation"] = (
            {"property": "offset_factor", "start_frame": animation_start, "end_frame": animation_end}
            if animation_start is not None
            else None
        )
        changed_resources = [curve.data.name] if created_curve else []
        root_action = getattr(getattr(root, "animation_data", None), "action", None)
        if root_action is not None:
            changed_resources.append(root_action.name)
        result["changed_resources"] = changed_resources
        return result

    def match_camera_transform(
        self,
        destination_name,
        policy="TRANSFORM_ONLY",
        source_object_name=None,
        world_transform=None,
    ):
        destination = _object(destination_name)
        if policy not in {"TRANSFORM_ONLY", "OPTICS_ONLY", "FULL"}:
            raise ValueError("Unsupported match policy")
        if (source_object_name is None) == (world_transform is None):
            raise ValueError("Supply exactly one source object or world transform")
        source = _object(source_object_name) if source_object_name is not None else None
        if policy != "TRANSFORM_ONLY" and (source is None or source.type != "CAMERA" or destination.type != "CAMERA"):
            raise ValueError("Optics matching requires source and destination camera objects")
        before = _transform_info(destination)
        optics = None
        if policy in {"TRANSFORM_ONLY", "FULL"}:
            if source is not None:
                desired_matrix = source.matrix_world.copy()
            else:
                location = _vector(world_transform.get("location"), "world_transform.location")
                scale = _vector(world_transform.get("scale", (1, 1, 1)), "world_transform.scale")
                quaternion_values = world_transform.get("rotation_quaternion")
                if quaternion_values is None or len(quaternion_values) != 4:
                    raise ValueError("world_transform.rotation_quaternion must contain four numbers")
                quaternion = mathutils.Quaternion(
                    tuple(_finite_number(value, "rotation_quaternion") for value in quaternion_values)
                )
                if quaternion.length_squared <= 1e-16:
                    raise ValueError("rotation_quaternion must not be zero")
                quaternion.normalize()
                desired_matrix = mathutils.Matrix.LocRotScale(list(location), quaternion, list(scale))
            destination.matrix_world = desired_matrix
        if policy in {"OPTICS_ONLY", "FULL"}:
            fields = [
                field
                for field in _OPTICS_COPY_FIELDS
                if hasattr(source.data, field) and hasattr(destination.data, field)
            ]
            optics = {
                "old": {field: _plain(getattr(destination.data, field)) for field in fields},
                "new": {field: _plain(getattr(source.data, field)) for field in fields},
            }
            destination.data.type = source.data.type
            for field in fields:
                setattr(destination.data, field, getattr(source.data, field))
        _update_view_layer()
        assigned = destination.matrix_world.copy()
        evaluated = destination.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.copy()
        return {
            "destination": destination.name,
            "source": getattr(source, "name", None),
            "policy": policy,
            "before": before,
            "after": _transform_info(destination),
            "optics": optics,
            "constraints_affect_evaluated_transform": not _matrix_close(assigned, evaluated),
            "changed_objects": [destination.name],
            "changed_resources": [destination.data.name] if policy != "TRANSFORM_ONLY" else [],
        }

    def duplicate_camera_rig(
        self,
        scene_name,
        source_root_name,
        collection_name,
        new_rig_name,
        camera_data_policy="COPY",
        path_data_policy="COPY",
        animation_policy="COPY",
        external_target_policy="SHARE",
    ):
        scene = _scene(scene_name)
        source_root = _object(source_root_name, scene=scene)
        rig_id = source_root.get("mcp_camera_rig_id")
        if not rig_id:
            raise ValueError(f"'{source_root.name}' is not tagged as an MCP camera rig")
        _required_name(new_rig_name, "new_rig_name")
        if camera_data_policy not in {"COPY", "LINK"} or path_data_policy not in {"COPY", "LINK"}:
            raise ValueError("Datablock policies must be COPY or LINK")
        if animation_policy not in {"COPY", "LINK", "NONE"}:
            raise ValueError("animation_policy must be COPY, LINK, or NONE")
        if external_target_policy not in {"SHARE", "REJECT"}:
            raise ValueError("external_target_policy must be SHARE or REJECT")
        members = [obj for obj in scene.objects if obj.get("mcp_camera_rig_id") == rig_id]
        if not members or len(members) > _MAX_RIG_MEMBERS:
            raise ValueError("Rig membership is empty or exceeds the 2000-object safety limit")
        member_set = set(members)
        external_objects = {
            constraint.target
            for obj in members
            for constraint in obj.constraints
            if getattr(constraint, "target", None) is not None and constraint.target not in member_set
        }
        external_objects.update(
            obj.parent for obj in members if obj.parent is not None and obj.parent not in member_set
        )
        for obj in members:
            animation = getattr(obj, "animation_data", None)
            for curve in getattr(animation, "drivers", ()) if animation is not None else ():
                for variable in curve.driver.variables:
                    for target in variable.targets:
                        if isinstance(target.id, bpy.types.Object) and target.id not in member_set:
                            external_objects.add(target.id)
        external = sorted(obj.name for obj in external_objects)
        if external and external_target_policy == "REJECT":
            raise ValueError(f"Rig has external constraint targets: {external}")
        collection = _ensure_collection(scene, collection_name)
        new_id = str(uuid.uuid4())
        mapping = {}
        used_names = set()
        for index, source in enumerate(sorted(members, key=lambda item: item.name)):
            clone = source.copy()
            role = source.get("mcp_camera_role", "member")
            stem = f"{new_rig_name} {str(role).replace('_', ' ').title()}"
            candidate = stem if stem not in used_names else f"{stem} {index + 1}"
            clone.name = candidate
            used_names.add(clone.name)
            if source.data is not None:
                copy_data = (source.type == "CAMERA" and camera_data_policy == "COPY") or (
                    source.type == "CURVE" and path_data_policy == "COPY"
                )
                if copy_data:
                    clone.data = source.data.copy()
            collection.objects.link(clone)
            _tag(clone, new_id, role)
            clone["mcp_camera_source_rig_id"] = rig_id
            mapping[source] = clone
        for source, clone in mapping.items():
            if source.parent in mapping:
                clone.parent = mapping[source.parent]
            for constraint in clone.constraints:
                target = getattr(constraint, "target", None)
                if target in mapping:
                    constraint.target = mapping[target]
            animation = getattr(clone, "animation_data", None)
            if animation is not None:
                for curve in getattr(animation, "drivers", ()):
                    for variable in curve.driver.variables:
                        for target in variable.targets:
                            if target.id in mapping:
                                target.id = mapping[target.id]
            _copy_action(clone, animation_policy)
            if clone.data is not None and clone.data is not source.data:
                _copy_action(clone.data, animation_policy)
        clones = list(mapping.values())
        new_root = mapping[source_root]
        cameras = [obj for obj in clones if obj.type == "CAMERA"]
        actions = sorted(
            {
                animation.action.name
                for obj in clones
                for owner in (obj, obj.data)
                if owner is not None
                for animation in [getattr(owner, "animation_data", None)]
                if animation is not None and animation.action is not None
            }
        )
        resources = [
            clone.data.name
            for source, clone in mapping.items()
            if clone.data is not None and clone.data is not source.data
        ]
        if animation_policy == "COPY":
            resources.extend(actions)
        return {
            "source_rig_id": rig_id,
            "rig_id": new_id,
            "root": new_root.name,
            "collection": collection.name,
            "members": [
                {"source": source.name, "name": clone.name, "role": clone.get("mcp_camera_role"), "type": clone.type}
                for source, clone in mapping.items()
            ],
            "cameras": [camera.name for camera in cameras],
            "external_targets": external,
            "sharing": {
                "camera_data": camera_data_policy,
                "path_data": path_data_policy,
                "animation": animation_policy,
                "external_targets": external_target_policy,
            },
            "actions": actions,
            "changed_objects": [obj.name for obj in clones],
            "changed_resources": list(dict.fromkeys([*resources, *actions])),
        }

    @staticmethod
    def _rig_result(rig_type, rig_id, collection, root, camera, members, constraints):
        _update_view_layer()
        return {
            "rig_type": rig_type,
            "rig_id": rig_id,
            "schema_version": _RIG_SCHEMA_VERSION,
            "collection": collection.name,
            "root": root.name,
            "camera": camera.name,
            "camera_data": camera.data.name,
            "members": [{"name": obj.name, "role": obj.get("mcp_camera_role"), "type": obj.type} for obj in members],
            "constraints": [_constraint_info(constraint) for constraint in constraints],
            "retained_live_dependencies": [
                getattr(constraint.target, "name", None)
                for constraint in constraints
                if getattr(constraint, "target", None) is not None
            ],
            "changed_objects": [obj.name for obj in members],
            "changed_resources": [camera.data.name],
        }
