# pyright: reportGeneralTypeIssues=false
"""Low-resolution physics rigs that drive preserved render objects."""

import contextlib
import math
import uuid

import bpy

from .inspection_and_setup import (
    _BODY_FIELDS,
    _add_rigid_body,
    _apply_patch,
    _body_info,
    _ensure_collection,
    _ensure_world,
    _preflight_collection_name,
    _prepare_cache_mutation,
    _remove_rigid_body,
    _scene,
    _validate_body_semantics,
    _validate_object_batch,
    _view_layer_for,
)


def _matrix_error(expected, actual):
    expected_location, expected_rotation, _expected_scale = expected.decompose()
    actual_location, actual_rotation, _actual_scale = actual.decompose()
    return {
        "position": float((expected_location - actual_location).length),
        "angle_radians": abs(float(expected_rotation.rotation_difference(actual_rotation).angle)),
    }


def _would_create_dependency_cycle(render, proxy):
    if render == proxy:
        return True
    return render in proxy.children_recursive or proxy in render.children_recursive


class RigidBodyProxyRigHandlers:
    """Build and validate mappings between physics proxies and render assets."""

    def create_rigid_body_proxy_rig(
        self,
        scene_name,
        rig_name,
        mappings,
        proxy_collection_name="Rigid Body Proxies",
        control_collection_name="Rigid Body Controls",
        render_collection_name="Rigid Body Render Assets",
        settings=None,
        verification_frames=None,
        transform_tolerance=0.001,
        confirm_delete_baked_cache=False,
    ):
        scene = _scene(scene_name)
        if not rig_name:
            raise ValueError("rig_name must be non-empty")
        if not mappings or len(mappings) > 64:
            raise ValueError("mappings must contain 1-64 entries")
        render_names = [mapping.get("render_object_name") for mapping in mappings]
        if len(render_names) != len(set(render_names)):
            raise ValueError("Render object mappings must be unique")
        renders = _validate_object_batch(scene, render_names)
        if any(obj.type != "MESH" for obj in renders):
            raise ValueError("Proxy rig render assets must be mesh objects")
        if any(obj.rigid_body is not None for obj in renders):
            configured = [obj.name for obj in renders if obj.rigid_body is not None]
            raise ValueError(f"Physics must live only on proxies; render objects have rigid bodies: {configured}")
        for name in (proxy_collection_name, control_collection_name, render_collection_name):
            _preflight_collection_name(scene, name)
        frames = list(verification_frames or [])
        if len(frames) > 5 or frames != sorted(set(frames)):
            raise ValueError("verification_frames must contain at most five unique ordered frames")
        if not math.isfinite(transform_tolerance) or not 0 < transform_tolerance <= 1:
            raise ValueError("transform_tolerance must be finite and in (0, 1]")
        shared_settings = dict(settings or {})
        if shared_settings.get("type", "ACTIVE") != "ACTIVE":
            raise ValueError("Proxy rig settings must use ACTIVE rigid bodies")
        explicit_names = [mapping.get("proxy_object_name") for mapping in mappings if mapping.get("proxy_object_name")]
        if len(explicit_names) != len(set(explicit_names)) or set(render_names) & set(explicit_names):
            raise ValueError("Explicit proxies must be unique and disjoint from render objects")
        generated_names = [
            f"{rig_name} - {mapping['render_object_name']} Proxy"
            for mapping in mappings
            if not mapping.get("proxy_object_name")
        ]
        collisions = [name for name in generated_names if bpy.data.objects.get(name) is not None]
        control_name = f"{rig_name} Control"
        if bpy.data.objects.get(control_name) is not None:
            collisions.append(control_name)
        if collisions:
            raise ValueError(f"Generated proxy-rig names already exist: {collisions}")
        resolved_existing = {}
        for mapping in mappings:
            proxy_name = mapping.get("proxy_object_name")
            if proxy_name:
                proxy = _validate_object_batch(scene, [proxy_name])[0]
                if proxy.type != "MESH" or proxy.rigid_body is not None:
                    raise ValueError(f"Existing proxy '{proxy.name}' must be an unconfigured mesh object")
                resolved_existing[proxy.name] = proxy
                render = bpy.data.objects[mapping["render_object_name"]]
                if _would_create_dependency_cycle(render, proxy):
                    raise ValueError(f"Mapping {render.name} -> {proxy.name} would create a dependency cycle")
                if mapping.get("driver", "COPY_TRANSFORMS") == "COPY_TRANSFORMS":
                    error = _matrix_error(proxy.matrix_world, render.matrix_world)
                    if max(error.values()) > transform_tolerance:
                        raise ValueError(
                            f"COPY_TRANSFORMS mapping '{render.name}' must begin aligned to proxy '{proxy.name}'"
                        )
        world = _ensure_world(scene)
        cache_freed = _prepare_cache_mutation(scene, world, confirm_delete_baked_cache)
        proxy_collection, _proxy_created = _ensure_collection(scene, proxy_collection_name)
        control_collection, _control_created = _ensure_collection(scene, control_collection_name)
        render_collection, _render_created = _ensure_collection(scene, render_collection_name)
        control = bpy.data.objects.new(control_name, None)
        control.empty_display_type = "PLAIN_AXES"
        control_collection.objects.link(control)
        rig_id = uuid.uuid4().hex
        control["blendermcp_rigid_body_rig_id"] = rig_id
        control["blendermcp_rigid_body_role"] = "proxy_rig_control"
        control["blendermcp_rigid_body_schema"] = 1
        created_proxies = []
        added_bodies = []
        added_constraints = []
        parent_snapshots = {}
        offsets = {}
        records = []
        render_by_name = {obj.name: obj for obj in renders}
        try:
            for mapping in mappings:
                render = render_by_name[mapping["render_object_name"]]
                proxy_name = mapping.get("proxy_object_name")
                if proxy_name:
                    proxy = resolved_existing[proxy_name]
                    if proxy.name not in proxy_collection.objects:
                        proxy_collection.objects.link(proxy)
                    _add_rigid_body(scene, proxy, "ACTIVE")
                    added_bodies.append(proxy)
                    approximation = mapping.get("approximation", "CONVEX_HULL")
                    shape = "CONVEX_HULL" if approximation == "LOW_RES_SOURCE" else approximation
                    patch = {**shared_settings, "type": "ACTIVE", "collision_shape": shape}
                    _validate_body_semantics(proxy.rigid_body, patch)
                    _apply_patch(proxy.rigid_body, patch, _BODY_FIELDS)
                    driver_type = mapping.get("driver", "COPY_TRANSFORMS")
                    if driver_type == "COPY_TRANSFORMS":
                        constraint = render.constraints.new("COPY_TRANSFORMS")
                        constraint.name = f"{rig_name} Proxy Driver"
                        constraint.target = proxy
                        constraint.mix_mode = "REPLACE"
                        added_constraints.append((render, constraint))
                    elif driver_type == "PARENT":
                        parent_snapshots[render.name] = (
                            render.parent,
                            render.matrix_parent_inverse.copy(),
                            render.matrix_world.copy(),
                        )
                        render_world = render.matrix_world.copy()
                        render.parent = proxy
                        render.matrix_parent_inverse = proxy.matrix_world.inverted()
                        render.matrix_world = render_world
                    else:
                        raise ValueError(f"Unsupported proxy driver: {driver_type}")
                else:
                    proxy_name = f"{rig_name} - {render.name} Proxy"
                    driver_type = mapping.get("driver", "COPY_TRANSFORMS")
                    if driver_type == "PARENT":
                        parent_snapshots[render.name] = (
                            render.parent,
                            render.matrix_parent_inverse.copy(),
                            render.matrix_world.copy(),
                        )
                    result = self.create_rigid_body_collision_proxy(
                        scene.name,
                        render.name,
                        proxy_name,
                        proxy_collection.name,
                        mapping.get("approximation", "CONVEX_HULL"),
                        "ACTIVE",
                        mapping.get("low_resolution_source_name"),
                        driver_type,
                        True,
                        shared_settings,
                        False,
                    )
                    proxy = bpy.data.objects[result["proxy"]]
                    created_proxies.append(proxy)
                    if driver_type == "COPY_TRANSFORMS" and result.get("driver"):
                        constraint = render.constraints.get(result["driver"]["name"])
                        if constraint is not None:
                            added_constraints.append((render, constraint))
                if render.name not in render_collection.objects:
                    render_collection.objects.link(render)
                if world.collection is not None and proxy.name not in world.collection.objects:
                    world.collection.objects.link(proxy)
                proxy["blendermcp_rigid_body_rig_id"] = rig_id
                proxy["blendermcp_rigid_body_role"] = "proxy_rig_physics"
                proxy["blendermcp_rigid_body_source"] = render.name
                proxy["blendermcp_rigid_body_schema"] = 1
                render["blendermcp_rigid_body_rig_id"] = rig_id
                render["blendermcp_rigid_body_role"] = "proxy_rig_render"
                render["blendermcp_rigid_body_proxy"] = proxy.name
                offsets[render.name] = proxy.matrix_world.inverted() @ render.matrix_world
                records.append(
                    {
                        "render_object": render.name,
                        "proxy_object": proxy.name,
                        "driver": mapping.get("driver", "COPY_TRANSFORMS"),
                        "generated_proxy": proxy in created_proxies,
                        "rigid_body": _body_info(proxy),
                    }
                )
        except Exception:
            for render, constraint in reversed(added_constraints):
                with contextlib.suppress(Exception):
                    render.constraints.remove(constraint)
            for render_name, (parent, inverse, matrix) in parent_snapshots.items():
                render = bpy.data.objects.get(render_name)
                if render is not None:
                    render.parent = parent
                    render.matrix_parent_inverse = inverse
                    render.matrix_world = matrix
            for proxy in reversed(added_bodies):
                with contextlib.suppress(Exception):
                    _remove_rigid_body(scene, proxy)
            for proxy in reversed(created_proxies):
                with contextlib.suppress(Exception):
                    bpy.data.objects.remove(proxy, do_unlink=True)
            with contextlib.suppress(Exception):
                bpy.data.objects.remove(control, do_unlink=True)
            raise
        original_frame = scene.frame_current
        original_subframe = scene.frame_subframe
        validation = []
        try:
            for frame in frames or [scene.frame_current]:
                scene.frame_set(frame)
                view_layer = _view_layer_for(scene)
                view_layer.update()
                depsgraph = view_layer.depsgraph
                for record in records:
                    render = bpy.data.objects[record["render_object"]].evaluated_get(depsgraph)
                    proxy = bpy.data.objects[record["proxy_object"]].evaluated_get(depsgraph)
                    expected = (
                        proxy.matrix_world
                        if record["driver"] == "COPY_TRANSFORMS"
                        else proxy.matrix_world @ offsets[record["render_object"]]
                    )
                    error = _matrix_error(expected, render.matrix_world)
                    validation.append({"frame": frame, **record, "transform_error": error})
        finally:
            scene.frame_set(original_frame, subframe=original_subframe)
            _view_layer_for(scene).update()
        exceeded = [item for item in validation if max(item["transform_error"].values()) > transform_tolerance]
        if exceeded:
            for render, constraint in reversed(added_constraints):
                with contextlib.suppress(Exception):
                    render.constraints.remove(constraint)
            for render_name, (parent, inverse, matrix) in parent_snapshots.items():
                render = bpy.data.objects.get(render_name)
                if render is not None:
                    render.parent = parent
                    render.matrix_parent_inverse = inverse
                    render.matrix_world = matrix
            for proxy in reversed(added_bodies):
                with contextlib.suppress(Exception):
                    _remove_rigid_body(scene, proxy)
            for proxy in reversed(created_proxies):
                with contextlib.suppress(Exception):
                    bpy.data.objects.remove(proxy, do_unlink=True)
            with contextlib.suppress(Exception):
                bpy.data.objects.remove(control, do_unlink=True)
            raise RuntimeError(f"Proxy driver verification exceeded tolerance: {exceeded[:3]}")
        return {
            "changed_objects": [control.name, *render_names, *[record["proxy_object"] for record in records]],
            "rig": rig_name,
            "rig_id": rig_id,
            "control": control.name,
            "collections": {
                "proxies": proxy_collection.name,
                "controls": control_collection.name,
                "render_assets": render_collection.name,
            },
            "mappings": records,
            "verification": validation,
            "transform_tolerance": transform_tolerance,
            "timeline_restored": {"frame": scene.frame_current, "subframe": scene.frame_subframe},
            "cache_freed": cache_freed,
            "warnings": [
                "Verification frames may populate Blender's temporary rigid-body cache.",
                *(["The protected rigid-body bake was explicitly freed."] if cache_freed else []),
            ],
        }
