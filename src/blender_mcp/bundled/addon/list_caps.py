"""
Every list cap a command accepts, in one table the dispatcher enforces before any handler runs.

The server states each cap as a pydantic `max_length` on a tool argument; the socket is the
boundary an unvalidated caller reaches, so the add-on has to enforce the same numbers - and it
cannot import the server package to read them. Each cap therefore lives here exactly once,
keyed by the command and the field's path in its params, and `refuse_oversized_lists` checks
all of a command's caps at dispatch. A handler holds no copy of its own: two copies are two
numbers that drift. `tests/test_size_cap_parity.py` sweeps every registered tool argument model
and fails on any capped list this table does not state at the same value, so a new capped list
on the server cannot ship without the add-on enforcing it.

A path names dict keys joined by "."; a list met on the way is entered element by element, so
`keys.poses` is the `poses` list of every record in `keys`.
"""

from collections.abc import Iterator

LIST_CAPS: dict[tuple[str, str], int] = {
    ("duplicate_or_instance_objects", "names"): 10_000,
    ("manage_object_hierarchy", "assignments"): 1_000,
    ("validate_scene", "scope"): 7,
    ("remove_scene_objects", "object_names"): 1_000,
    ("keyframe_object_transform", "keyframes"): 500,
    ("edit_keyframes", "edits"): 1_000,
    ("bake_evaluated_animation", "target.transforms"): 3,
    ("bake_evaluated_animation", "target.bone_names"): 10_000,
    ("bake_evaluated_animation", "target.properties"): 1_000,
    ("bake_evaluated_animation", "target.properties.array_indices"): 64,
    ("manage_animation_driver", "variables"): 64,
    ("clear_materials", "object_names"): 500,
    ("sync_data_name", "object_names"): 500,
    ("keyframe_camera_rig", "keyframes"): 500,
    ("validate_camera_rig", "object_names"): 500,
    ("validate_camera_rig", "sample_frames"): 24,
    ("create_camera_markers", "markers"): 200,
    ("frame_camera_on_objects", "bone_targets"): 64,
    ("frame_camera_on_objects", "armature_names"): 16,
    ("create_geometry_object", "geometry.vertices"): 1_000_000,
    ("create_geometry_object", "geometry.edges"): 2_000_000,
    ("create_geometry_object", "geometry.faces"): 1_000_000,
    ("create_geometry_object", "geometry.splines"): 10_000,
    ("create_geometry_object", "geometry.splines.points"): 100_000,
    ("create_geometry_object", "geometry.elements"): 10_000,
    ("create_geometry_object", "geometry.points"): 2_000_000,
    ("create_geometry_object", "geometry.curve_sizes"): 1_000_000,
    ("create_geometry_object", "geometry.attributes"): 256,
    ("create_geometry_object", "geometry.attributes.values"): 2_000_000,
    ("create_geometry_object", "geometry.layers"): 10_000,
    ("create_geometry_object", "geometry.layers.frames"): 100_000,
    ("create_geometry_object", "geometry.layers.frames.strokes"): 100_000,
    ("create_geometry_object", "geometry.layers.frames.strokes.points"): 100_000,
    ("create_geometry_object", "geometry.layers.frames.attributes"): 256,
    ("create_geometry_object", "geometry.layers.frames.attributes.values"): 2_000_000,
    ("validate_liquid_result", "frames"): 32,
    ("setup_liquid_shot", "containers"): 16,
    ("setup_liquid_shot", "sources"): 16,
    ("sample_liquid_simulation", "frames"): 32,
    ("get_rigid_body_object_info", "object_names"): 100,
    ("get_rigid_body_constraint_info", "constraint_object_names"): 500,
    ("add_rigid_bodies", "object_names"): 500,
    ("configure_rigid_bodies", "targets"): 500,
    ("set_rigid_body_mass", "assignments"): 500,
    ("set_rigid_body_collision_layers", "targets"): 500,
    ("set_rigid_body_collision_layers", "targets.layers"): 20,
    ("validate_rigid_body_setup", "object_names"): 500,
    ("create_compound_rigid_body", "child_object_names"): 128,
    ("create_rigid_body_constraint_network", "body_names"): 256,
    ("create_rigid_body_constraint_network", "edges"): 512,
    ("prepare_fracture_rigid_bodies", "piece_object_names"): 500,
    ("create_rigid_body_chain", "body_names"): 256,
    ("setup_animated_passive_collider", "sample_frames"): 32,
    ("create_rigid_body_debris_field", "sources"): 32,
    ("create_rigid_body_debris_field", "collision_layers"): 20,
    ("bake_rigid_bodies_to_keyframes", "object_names"): 100,
    ("export_rigid_body_animation", "object_names"): 100,
    ("configure_rigid_body_force_fields", "fields"): 64,
    ("remove_rigid_body_components", "object_names"): 500,
    ("analyze_rigid_body_performance", "object_names"): 500,
    ("analyze_rigid_body_performance", "sample_frames"): 20,
    ("create_rigid_body_proxy_rig", "mappings"): 64,
    ("create_rigid_body_proxy_rig", "verification_frames"): 5,
    ("create_ragdoll_rig", "bodies"): 64,
    ("create_ragdoll_rig", "joints"): 128,
    ("create_ragdoll_rig", "collision_layers"): 20,
    ("bake_ragdoll_to_armature", "mappings"): 64,
    ("sample_rigid_body_simulation", "object_names"): 100,
    ("sample_rigid_body_simulation", "frame_selection.frames"): 100,
    ("get_scene_physics_info", "convert_seconds"): 32,
    ("edit_node_group_interface", "edits"): 200,
    ("patch_geometry_node_graph", "operations"): 500,
    ("set_geometry_nodes_inputs", "targets"): 200,
    ("set_geometry_nodes_inputs", "targets.inputs"): 200,
    ("analyze_procedural_performance", "frames"): 8,
    ("create_repeat_zone", "state_items"): 32,
    ("create_repeat_zone", "graph_operations"): 200,
    ("create_simulation_zone", "state_items"): 32,
    ("create_simulation_zone", "graph_operations"): 200,
    ("nd_mark_as_util", "object_names"): 500,
    ("nd_create_id_material", "object_names"): 500,
    ("nd_bulk_create_id_materials", "object_names"): 500,
    ("nd_set_lod_suffix", "object_names"): 500,
    ("nd_apply_modifiers", "object_names"): 500,
    ("get_character_rig_info", "bone_names"): 200,
    ("get_skinning_info", "mesh_object_names"): 200,
    ("create_armature", "bones"): 1_000,
    ("create_armature", "bones.collections"): 64,
    ("patch_armature_bones", "operations"): 1_000,
    ("patch_armature_bones", "operations.collections"): 64,
    ("mirror_armature_bones", "bone_names"): 500,
    ("manage_bone_collections", "operations"): 500,
    ("manage_bone_collections", "operations.bone_names"): 500,
    ("configure_armature_bones", "bone_patches"): 1_000,
    ("configure_armature_bones", "pose_bone_patches"): 1_000,
    ("bind_mesh_to_armature", "mesh_object_names"): 200,
    ("set_skin_weights", "assignments"): 2_000,
    ("set_skin_weights", "assignments.vertex_indices"): 100_000,
    ("set_skin_weights", "normalized_vertices"): 10_000,
    ("set_skin_weights", "normalized_vertices.weights"): 256,
    ("clean_skin_weights", "vertex_indices"): 100_000,
    ("clean_skin_weights", "protected_group_names"): 500,
    ("validate_character_rig", "armature_object_names"): 200,
    ("validate_character_rig", "mesh_object_names"): 200,
    ("validate_character_rig", "frames"): 50,
    ("create_ik_chain", "chain_bone_names"): 64,
    ("create_ik_fk_limb", "deform_bone_names"): 16,
    ("create_spline_ik_rig", "chain_bone_names"): 256,
    ("create_spline_ik_rig", "curve_points"): 256,
    ("create_rig_property_driver", "destinations"): 100,
    ("assign_bone_custom_shapes", "assignments"): 500,
    ("create_shape_key_controls", "controls"): 200,
    ("create_shape_key_controls", "controls.inputs"): 8,
    ("configure_bendy_bones", "patches"): 500,
    ("list_character_bones", "bone_names"): 200,
    ("probe_bone_axis", "axes"): 6,
    ("probe_bone_axis", "reference_directions"): 6,
    ("set_character_pose", "poses"): 500,
    ("keyframe_character_pose", "poses"): 500,
    ("keyframe_character_pose", "keys"): 250,
    ("keyframe_character_pose", "keys.poses"): 500,
    ("solve_bone_reach", "reaches"): 8,
    ("keyframe_bone_reach", "reaches"): 8,
    ("keyframe_bone_reach", "reaches.keys"): 250,
    ("sample_deformed_geometry", "vertex_indices"): 1_000,
    ("sample_evaluated_range", "frames"): 250,
    ("sample_evaluated_range", "bone_points"): 32,
    ("sample_evaluated_range", "mesh_metrics.object_names"): 16,
    ("sample_evaluated_range", "mesh_metrics.against_object_names"): 16,
    ("render_contact_sheet", "cells"): 30,
    ("validate_lighting_setup", "subject_object_names"): 100,
    ("patch_shader_graph", "operations"): 500,
}


def _grouped_by_command() -> dict[str, list[tuple[tuple[str, ...], int]]]:
    """
    Group `LIST_CAPS` by command, each path split into its keys, so dispatch reads only its own rows.

    Returns:
        dict[str, list[tuple[tuple[str, ...], int]]]: Each command's `(path keys, cap)` pairs.

    """
    grouped: dict[str, list[tuple[tuple[str, ...], int]]] = {}
    for (command, path), cap in LIST_CAPS.items():
        grouped.setdefault(command, []).append((tuple(path.split(".")), cap))
    return grouped


_CAPS_BY_COMMAND = _grouped_by_command()


def _oversized(value: object, steps: tuple[str, ...], label: str, cap: int) -> Iterator[tuple[str, int]]:
    """
    Yield every list or mapping at the end of `steps` that holds more than `cap` entries.

    Args:
        value: The params, or the part of them reached so far.
        steps: The dict keys still to follow.
        label: Where `value` sits, for the refusal to name, e.g. `keys[3]`.
        cap: The most entries the list at the end of the path may hold.

    Yields:
        tuple[str, int]: The oversized field's location and its length.

    """
    if not steps:
        if isinstance(value, (list, tuple, dict)) and len(value) > cap:
            yield label, len(value)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _oversized(item, steps, f"{label}[{index}]", cap)
    elif isinstance(value, dict) and steps[0] in value:
        head = steps[0]
        yield from _oversized(value[head], steps[1:], f"{label}.{head}" if label else head, cap)


def refuse_oversized_lists(command: str, params: object) -> None:
    """
    Refuse a command whose params carry a list longer than the cap stated for it.

    Args:
        command: The command type being dispatched.
        params: Its params, as received off the socket.

    Raises:
        ValueError: Naming the first oversized field, its length and its cap.

    """
    for steps, cap in _CAPS_BY_COMMAND.get(command, ()):
        for label, length in _oversized(params, steps, "", cap):
            raise ValueError(f"{command}: {label} carries {length:,} entries, more than the {cap:,} one call accepts")
