"""
The one command registry: every command the add-on dispatches, and how each is classified.

A command's row decides whether it runs inside `mutation_transaction`, whether it backs
up mesh data, whether it swaps the database or ends the drain tick, whether it may run
while the session is indeterminate, and which optional integration gates it. The target
tables decide which existing objects a mutating request's rollback protects.
"""

import itertools

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

import bpy

# The optional integrations whose commands are gated on a per-.blend scene flag.
Provider = Literal["polyhaven", "sketchfab", "nd"]


def _action(params: Mapping[str, object], default: str) -> str:
    """
    Read a command's `action` parameter the way its handler will.

    Args:
        params: The command's params.
        default: The handler's own default for `action`, so a call that omits
            it is routed as the handler will treat it.

    Returns:
        str: The action, upper-cased.

    """
    return str(params.get("action", default)).upper()


# Naming any of these turns `manage_procedural_instances` from an inspection
# into an edit of the instancer, so the call has to be transacted.
_PROCEDURAL_INSTANCE_MUTATION_PARAMS = (
    "source_type",
    "source_name",
    "pick_instance",
    "rotation",
    "scale",
    "translation",
    "realize_instances",
)


# One command, one row: how it dispatches and every routing decision made about it.
# Before this table each of those decisions lived in its own name-keyed set, nothing
# coupled them to the dispatch keys, and a name left out of one of them - the geometry
# set especially - silently turned that protection off.
@dataclass(frozen=True, slots=True)
class CommandSpec:
    """
    How one command is classified, for every decision the dispatcher makes about it.

    Attributes:
        read_only: Never mutates `bpy.data`, whatever its params, so it skips
            `mutation_transaction` entirely - a snapshot, a diff and an undo
            checkpoint would all buy nothing.
        read_only_when: Read-only for *some* params only: an `INSPECT` cache
            call, a dry-run sewing preview, a conformity analysis asked for no
            heat map. Answers "is *this* call read-only?" from its params alone.
        non_undo: Mutates, but nothing worth an undo checkpoint - viewport and
            capture toggles, and the playhead. Transacting them would only add
            undo-stack noise.
        non_undo_when: The same, decided per call. A render writes a file rather
            than scene state - unless `persist_output` stores its output template
            on the scene, which is scene state a rollback must restore.
        geometry: Edits an existing object's mesh, so the transaction backs the
            mesh datablock up (a full copy) and swaps it back on failure.
            Transform-only commands skip that cost.
        session_swap: Replaces Blender's whole database. The drain loop discards
            the queue behind it and ends its tick, and it never enters a
            transaction: after a load every id looks new and a rollback would
            remove the whole file. `save_shot` is not one - a save replaces no
            datablock, so the commands queued behind it are still valid.
        datablock_replacing: Replaces or frees linked datablocks in place. A
            reload gives them new session_uids, so a transaction would treat them
            as created by the request and delete them on rollback. Not a swap.
        tick_ending: The drain loop ends its tick after this command, leaving the
            queue for the next one. Blender clears `is_dirty` for a save only
            after the tick returns, so an edit later in the same tick would lose
            its dirty flag and `open_shot`'s unsaved-work guard would let the
            work be thrown away.
        indeterminate_safe: Still runs while the session is indeterminate,
            because it either repairs that condition or publishes
            `session_indeterminate`, the reason for every other refusal.
        provider: The optional integration whose scene flag gates this command,
            or None for one that is always dispatchable.

    """

    read_only: bool = False
    read_only_when: Callable[[Mapping[str, object]], bool] | None = None
    non_undo: bool = False
    non_undo_when: Callable[[Mapping[str, object]], bool] | None = None
    geometry: bool = False
    session_swap: bool = False
    datablock_replacing: bool = False
    tick_ending: bool = False
    indeterminate_safe: bool = False
    provider: Provider | None = None


# What an unregistered command name resolves to: dispatch refuses it by name, and
# every classification below defaults to the safe answer - mutating, transacted.
_UNCLASSIFIED = CommandSpec()

COMMANDS: Mapping[str, CommandSpec] = MappingProxyType(
    {
        "ping": CommandSpec(read_only=True),
        "list_scene_objects": CommandSpec(read_only=True),
        "get_addon_info": CommandSpec(read_only=True, indeterminate_safe=True),
        "get_session_info": CommandSpec(read_only=True, indeterminate_safe=True),
        "open_shot": CommandSpec(session_swap=True, indeterminate_safe=True),
        "save_shot": CommandSpec(tick_ending=True),
        "reset_session": CommandSpec(session_swap=True, indeterminate_safe=True),
        "link_canon_library": CommandSpec(),
        "create_override": CommandSpec(),
        "list_libraries": CommandSpec(read_only=True),
        "inspect_delivery": CommandSpec(read_only=True),
        "reload_library": CommandSpec(datablock_replacing=True),
        "relocate_library": CommandSpec(datablock_replacing=True),
        "unlink_libraries": CommandSpec(datablock_replacing=True),
        "get_object_info": CommandSpec(read_only=True),
        "get_mesh_data": CommandSpec(read_only=True),
        "inspect_animation": CommandSpec(read_only=True),
        "manage_animation_action": CommandSpec(),
        "edit_keyframes": CommandSpec(),
        # `operation`, not `action`: INSPECT reads every selected curve's key extent and
        # writes no modifier, so a snapshot and an undo checkpoint would buy nothing.
        "set_action_cycle": CommandSpec(
            read_only_when=lambda params: str(params.get("operation", "SET")).upper() == "INSPECT"
        ),
        "bake_evaluated_animation": CommandSpec(),
        "manage_nla_tracks": CommandSpec(),
        "manage_animation_driver": CommandSpec(),
        "list_procedural_systems": CommandSpec(read_only=True),
        "get_geometry_node_graph": CommandSpec(read_only=True),
        "get_geometry_node_type_info": CommandSpec(read_only=True),
        "create_geometry_node_group": CommandSpec(),
        "attach_geometry_nodes_modifier": CommandSpec(),
        "edit_node_group_interface": CommandSpec(),
        "patch_geometry_node_graph": CommandSpec(),
        "set_geometry_nodes_inputs": CommandSpec(),
        "manage_geometry_nodes_modifier": CommandSpec(),
        "copy_geometry_node_group": CommandSpec(),
        "inspect_evaluated_geometry": CommandSpec(read_only=True),
        "validate_geometry_node_graph": CommandSpec(read_only=True),
        "create_procedural_scatter": CommandSpec(),
        "create_curve_generator": CommandSpec(),
        "create_procedural_array": CommandSpec(),
        "create_surface_paneling": CommandSpec(),
        "create_procedural_boolean": CommandSpec(),
        "create_procedural_deformer": CommandSpec(),
        "create_volume_generator": CommandSpec(),
        "manage_named_attributes": CommandSpec(
            geometry=True, read_only_when=lambda params: _action(params, "LIST") == "LIST"
        ),
        "manage_procedural_instances": CommandSpec(
            read_only_when=lambda params: (
                not any(params.get(key) is not None for key in _PROCEDURAL_INSTANCE_MUTATION_PARAMS)
            )
        ),
        "run_geometry_nodes_tool": CommandSpec(geometry=True),
        "publish_procedural_asset": CommandSpec(),
        "create_repeat_zone": CommandSpec(),
        "create_simulation_zone": CommandSpec(),
        "manage_geometry_nodes_bake": CommandSpec(
            read_only_when=lambda params: _action(params, "INSPECT") == "INSPECT"
        ),
        "realize_procedural_output": CommandSpec(),
        "analyze_procedural_performance": CommandSpec(read_only=True),
        "get_viewport_screenshot": CommandSpec(read_only=True),
        "create_geometry_object": CommandSpec(),
        "set_object_transform": CommandSpec(),
        "set_scene_frame": CommandSpec(non_undo=True),
        "duplicate_or_instance_objects": CommandSpec(),
        "manage_scene_collections": CommandSpec(),
        "manage_object_hierarchy": CommandSpec(),
        "manage_object_constraints": CommandSpec(),
        "manage_modifiers": CommandSpec(),
        "set_object_visibility": CommandSpec(),
        "remove_scene_objects": CommandSpec(),
        "reset_scene": CommandSpec(),
        "validate_scene": CommandSpec(read_only=True),
        "inspect_render_setup": CommandSpec(read_only=True),
        "configure_render_settings": CommandSpec(),
        "get_scene_physics_info": CommandSpec(read_only=True),
        "configure_scene_physics": CommandSpec(),
        "keyframe_object_transform": CommandSpec(),
        "manage_view_layers": CommandSpec(),
        "plan_render_animation": CommandSpec(read_only=True),
        "render_scene": CommandSpec(non_undo_when=lambda params: not params.get("persist_output", False)),
        "inspect_render_output": CommandSpec(read_only=True),
        # CREATE saves a copy of the file and starts a separate Blender; it changes nothing in
        # this session's bpy.data, so there is nothing to snapshot or undo. READ, LIST and DELETE
        # only read and remove job files.
        "manage_render_job": CommandSpec(non_undo=True, read_only_when=lambda params: _action(params, "") != "CREATE"),
        "get_polyhaven_status": CommandSpec(read_only=True),
        "get_nd_status": CommandSpec(read_only=True),
        "get_sketchfab_status": CommandSpec(read_only=True),
        "create_primitive": CommandSpec(),
        "mesh_extrude": CommandSpec(geometry=True),
        "mesh_inset": CommandSpec(geometry=True),
        "mesh_bevel": CommandSpec(geometry=True),
        "mesh_bridge": CommandSpec(geometry=True),
        "mesh_boolean": CommandSpec(geometry=True),
        "mesh_subdivide": CommandSpec(geometry=True),
        "mesh_remesh": CommandSpec(geometry=True),
        "mesh_solidify": CommandSpec(geometry=True),
        "mesh_symmetrize": CommandSpec(geometry=True),
        "create_retopology_target": CommandSpec(),
        "inspect_retopology": CommandSpec(read_only=True),
        "analyze_surface_conformity": CommandSpec(
            geometry=True, read_only_when=lambda params: not params.get("create_heat_map", False)
        ),
        "manage_retopology_checkpoint": CommandSpec(
            geometry=True, read_only_when=lambda params: _action(params, "") in {"LIST", "COMPARE"}
        ),
        "configure_surface_projection": CommandSpec(geometry=True),
        "project_mesh_elements": CommandSpec(geometry=True),
        "build_quad_patch": CommandSpec(geometry=True),
        "extend_boundary": CommandSpec(geometry=True),
        "fill_boundary_quads": CommandSpec(geometry=True),
        "reroute_topology": CommandSpec(geometry=True),
        "relax_topology": CommandSpec(geometry=True),
        "redistribute_edge_loop": CommandSpec(geometry=True),
        "configure_retopology_symmetry": CommandSpec(),
        "validate_retopology": CommandSpec(read_only=True),
        "create_retopology_guides": CommandSpec(),
        "create_surface_section": CommandSpec(),
        "set_retopology_features": CommandSpec(geometry=True),
        "add_support_loops": CommandSpec(geometry=True),
        "transfer_mesh_attributes": CommandSpec(geometry=True),
        "unwrap_retopology_uvs": CommandSpec(geometry=True),
        "create_bake_cage": CommandSpec(),
        "bake_retopology_maps": CommandSpec(),
        "test_deformation": CommandSpec(read_only=True),
        "generate_quadriflow_draft": CommandSpec(),
        "fit_surface_primitive": CommandSpec(),
        "bind_surface_deformation": CommandSpec(),
        "generate_retopology_lods": CommandSpec(),
        "copy_object_transform": CommandSpec(),
        "add_radial_array_modifier": CommandSpec(),
        "set_viewport_overlay": CommandSpec(non_undo=True),
        "clear_materials": CommandSpec(),
        "clear_vertex_groups": CommandSpec(),
        "clear_edge_marks": CommandSpec(),
        "sync_data_name": CommandSpec(),
        "get_character_rig_info": CommandSpec(read_only=True),
        "get_skinning_info": CommandSpec(read_only=True),
        # Read-only like `validate_character_rig`: it moves the playhead to the requested
        # frame and puts it back, and `to_mesh()` output is released before it returns.
        "sample_deformed_geometry": CommandSpec(read_only=True),
        "create_armature": CommandSpec(),
        "patch_armature_bones": CommandSpec(),
        "mirror_armature_bones": CommandSpec(),
        "manage_bone_collections": CommandSpec(),
        "configure_armature_bones": CommandSpec(),
        "bind_mesh_to_armature": CommandSpec(),
        "set_skin_weights": CommandSpec(),
        "clean_skin_weights": CommandSpec(),
        "add_pose_bone_constraint": CommandSpec(),
        "validate_character_rig": CommandSpec(read_only=True),
        "transfer_skin_weights": CommandSpec(),
        "create_ik_chain": CommandSpec(),
        "create_ik_fk_limb": CommandSpec(),
        "create_spline_ik_rig": CommandSpec(),
        "configure_bendy_bones": CommandSpec(),
        "create_rig_property_driver": CommandSpec(),
        "assign_bone_custom_shapes": CommandSpec(),
        "list_character_bones": CommandSpec(read_only=True),
        # Read-only like `validate_character_rig`, which also samples frames it puts back: the
        # trial rotation is restored by `restored_bone_pose` before the handler returns, so
        # there is no net mutation for a transaction to snapshot.
        "probe_bone_axis": CommandSpec(read_only=True),
        "set_character_pose": CommandSpec(),
        "keyframe_character_pose": CommandSpec(),
        "solve_bone_reach": CommandSpec(),
        "keyframe_bone_reach": CommandSpec(),
        "create_shape_key_controls": CommandSpec(),
        "get_rigid_body_scene_info": CommandSpec(read_only=True),
        "get_rigid_body_object_info": CommandSpec(read_only=True),
        "get_rigid_body_constraint_info": CommandSpec(read_only=True),
        "configure_rigid_body_world": CommandSpec(),
        "add_rigid_bodies": CommandSpec(),
        "configure_rigid_bodies": CommandSpec(),
        "set_rigid_body_mass": CommandSpec(),
        "set_rigid_body_collision_layers": CommandSpec(),
        "create_rigid_body_collision_proxy": CommandSpec(),
        "create_rigid_body_constraint": CommandSpec(),
        "configure_rigid_body_constraint": CommandSpec(),
        "validate_rigid_body_setup": CommandSpec(read_only=True),
        "remove_rigid_body_components": CommandSpec(),
        "animate_rigid_body_release": CommandSpec(),
        "create_compound_rigid_body": CommandSpec(),
        "create_rigid_body_constraint_network": CommandSpec(),
        "prepare_fracture_rigid_bodies": CommandSpec(),
        "create_rigid_body_chain": CommandSpec(),
        "setup_animated_passive_collider": CommandSpec(),
        "configure_rigid_body_force_fields": CommandSpec(),
        "sample_rigid_body_simulation": CommandSpec(),
        "manage_rigid_body_cache": CommandSpec(read_only_when=lambda params: _action(params, "INSPECT") == "INSPECT"),
        "bake_rigid_bodies_to_keyframes": CommandSpec(),
        "create_rigid_body_debris_field": CommandSpec(),
        "create_rigid_body_proxy_rig": CommandSpec(),
        "create_ragdoll_rig": CommandSpec(),
        "bake_ragdoll_to_armature": CommandSpec(),
        "export_rigid_body_animation": CommandSpec(),
        "analyze_rigid_body_performance": CommandSpec(read_only_when=lambda params: not params.get("sample_frames")),
        "get_cloth_simulation_info": CommandSpec(read_only=True),
        "get_cloth_object_info": CommandSpec(read_only=True),
        "get_liquid_simulation_info": CommandSpec(read_only=True),
        "get_fluid_object_info": CommandSpec(read_only=True),
        "inspect_fluid_simulation": CommandSpec(read_only=True),
        "create_fluid_domain": CommandSpec(),
        "configure_fluid_solver": CommandSpec(),
        "add_fluid_flow": CommandSpec(),
        "add_fluid_effector": CommandSpec(),
        "manage_fluid_cache": CommandSpec(),
        "get_camera_rig_info": CommandSpec(read_only=True),
        "create_camera": CommandSpec(),
        "configure_camera": CommandSpec(),
        "set_scene_camera": CommandSpec(),
        "point_camera_at": CommandSpec(),
        "create_camera_target": CommandSpec(),
        "frame_camera_on_objects": CommandSpec(),
        "create_orbit_camera_rig": CommandSpec(),
        "create_dolly_camera_rig": CommandSpec(),
        "create_crane_camera_rig": CommandSpec(),
        "create_camera_path_rig": CommandSpec(),
        "configure_camera_dof": CommandSpec(),
        "keyframe_camera_rig": CommandSpec(),
        "set_camera_interpolation": CommandSpec(),
        "create_focus_pull": CommandSpec(),
        "create_dolly_zoom": CommandSpec(),
        "add_camera_shake": CommandSpec(),
        "create_camera_markers": CommandSpec(read_only_when=lambda params: _action(params, "") == "LIST"),
        "match_camera_transform": CommandSpec(),
        "duplicate_camera_rig": CommandSpec(),
        "add_camera_constraint": CommandSpec(),
        "configure_camera_render_gate": CommandSpec(),
        "validate_camera_rig": CommandSpec(read_only=True),
        "list_lights": CommandSpec(read_only=True),
        "inspect_light": CommandSpec(read_only=True),
        "inspect_lighting_setup": CommandSpec(read_only=True),
        "validate_lighting_setup": CommandSpec(read_only=True),
        "create_light": CommandSpec(),
        "configure_light": CommandSpec(),
        "aim_light": CommandSpec(),
        "configure_light_linking": CommandSpec(),
        "create_studio_lighting": CommandSpec(),
        "configure_world_background": CommandSpec(),
        "configure_hdri_environment": CommandSpec(),
        "configure_procedural_sky": CommandSpec(),
        "configure_lighting_quality": CommandSpec(),
        "configure_color_management": CommandSpec(),
        "render_lighting_preview": CommandSpec(),
        "list_materials": CommandSpec(read_only=True),
        "inspect_material": CommandSpec(read_only=True),
        "get_shader_node_type_info": CommandSpec(read_only=True),
        "patch_shader_graph": CommandSpec(),
        "create_pbr_material": CommandSpec(),
        "configure_pbr_material": CommandSpec(),
        "assign_material": CommandSpec(),
        "configure_texture_mapping": CommandSpec(),
        "list_texture_images": CommandSpec(read_only=True),
        "load_texture_image": CommandSpec(),
        "configure_texture_image": CommandSpec(),
        "apply_pbr_texture_set": CommandSpec(),
        "save_texture_image": CommandSpec(),
        "render_pbr_material_preview": CommandSpec(),
        "manage_uv_maps": CommandSpec(),
        "set_uv_seams": CommandSpec(),
        "unwrap_uvs": CommandSpec(),
        "optimize_uv_layout": CommandSpec(),
        "inspect_uv_layout": CommandSpec(read_only=True),
        "bake_texture_map": CommandSpec(),
        "validate_pbr_asset": CommandSpec(read_only=True),
        "add_cloth_simulation": CommandSpec(),
        "configure_cloth_material": CommandSpec(),
        "configure_cloth_solver": CommandSpec(),
        "set_cloth_vertex_weights": CommandSpec(),
        "configure_cloth_pinning": CommandSpec(),
        "configure_cloth_collisions": CommandSpec(),
        "add_cloth_collider": CommandSpec(),
        "configure_cloth_collider": CommandSpec(),
        "estimate_cloth_resources": CommandSpec(read_only=True),
        "validate_cloth_setup": CommandSpec(read_only=True),
        "configure_cloth_sewing": CommandSpec(
            geometry=True, read_only_when=lambda params: bool(params.get("dry_run", True))
        ),
        "configure_cloth_pressure": CommandSpec(),
        "configure_cloth_internal_springs": CommandSpec(),
        "configure_cloth_rest_shape": CommandSpec(),
        "configure_cloth_field_weights": CommandSpec(),
        "animate_cloth_parameters": CommandSpec(),
        "create_cloth_attachment": CommandSpec(),
        "create_character_cloth_setup": CommandSpec(),
        "sample_cloth_simulation": CommandSpec(),
        "manage_cloth_cache": CommandSpec(read_only_when=lambda params: _action(params, "INSPECT") == "INSPECT"),
        "remove_cloth_components": CommandSpec(),
        "create_cloth_proxy_rig": CommandSpec(),
        "duplicate_cloth_setup_variant": CommandSpec(),
        "prepare_cloth_render_surface": CommandSpec(),
        "export_cloth_simulation": CommandSpec(),
        "analyze_cloth_performance": CommandSpec(),
        "create_liquid_domain": CommandSpec(),
        "fit_liquid_domain": CommandSpec(geometry=True),
        "configure_liquid_solver": CommandSpec(),
        "add_liquid_flow": CommandSpec(),
        "configure_liquid_flow": CommandSpec(),
        "add_liquid_effector": CommandSpec(),
        "configure_liquid_effector": CommandSpec(),
        "configure_liquid_scope_and_boundaries": CommandSpec(),
        "estimate_liquid_resources": CommandSpec(read_only=True),
        "validate_liquid_setup": CommandSpec(read_only=True),
        "configure_liquid_mesh": CommandSpec(),
        "apply_liquid_quality_profile": CommandSpec(),
        "configure_liquid_secondary_particles": CommandSpec(),
        "configure_liquid_diffusion": CommandSpec(),
        "animate_liquid_flow": CommandSpec(),
        "create_liquid_guide": CommandSpec(),
        "configure_liquid_force_fields": CommandSpec(),
        "create_liquid_material": CommandSpec(),
        "create_secondary_particle_render_setup": CommandSpec(),
        "sample_liquid_simulation": CommandSpec(),
        "manage_liquid_cache": CommandSpec(read_only_when=lambda params: _action(params, "STATUS") == "STATUS"),
        "remove_fluid_components": CommandSpec(),
        "create_liquid_proxy_rig": CommandSpec(),
        "duplicate_liquid_setup_variant": CommandSpec(),
        "prepare_liquid_render_mesh": CommandSpec(),
        "export_liquid_simulation": CommandSpec(),
        "analyze_liquid_performance": CommandSpec(
            read_only_when=lambda params: not params.get("measure_replay_evaluation", False)
        ),
        "setup_liquid_shot": CommandSpec(),
        "validate_liquid_result": CommandSpec(),
        "get_polyhaven_categories": CommandSpec(read_only=True, provider="polyhaven"),
        "list_polyhaven_assets": CommandSpec(read_only=True, provider="polyhaven"),
        "import_polyhaven_asset": CommandSpec(provider="polyhaven"),
        "apply_polyhaven_texture": CommandSpec(provider="polyhaven"),
        "search_sketchfab_models": CommandSpec(read_only=True, provider="sketchfab"),
        "get_sketchfab_model_preview": CommandSpec(read_only=True, provider="sketchfab"),
        "import_sketchfab_model": CommandSpec(provider="sketchfab"),
        "nd_boolean": CommandSpec(provider="nd"),
        "nd_mark_as_util": CommandSpec(provider="nd"),
        "nd_clean_utils": CommandSpec(provider="nd"),
        "nd_create_id_material": CommandSpec(provider="nd"),
        "nd_bulk_create_id_materials": CommandSpec(provider="nd"),
        "nd_set_lod_suffix": CommandSpec(provider="nd"),
        "nd_single_vertex": CommandSpec(provider="nd"),
        "nd_apply_modifiers": CommandSpec(provider="nd"),
        "nd_pulse_viewport_toggle": CommandSpec(non_undo=True, provider="nd"),
        "nd_capture_utils": CommandSpec(non_undo=True, provider="nd"),
    }
)

# Which scene flag turns each provider's commands on. Read per call, because the
# flags belong to the open .blend and change when another file is loaded.
_PROVIDER_SCENE_FLAGS: Mapping[Provider, str] = MappingProxyType(
    {
        "polyhaven": "blendermcp_use_polyhaven",
        "sketchfab": "blendermcp_use_sketchfab",
        "nd": "blendermcp_use_nd",
    }
)

# The dispatch table's two halves, partitioned once at import rather than per command.
_UNGATED_COMMAND_NAMES: tuple[str, ...] = tuple(name for name, spec in COMMANDS.items() if spec.provider is None)
_GATED_COMMAND_NAMES: Mapping[Provider, tuple[str, ...]] = MappingProxyType(
    {
        provider: tuple(name for name, spec in COMMANDS.items() if spec.provider == provider)
        for provider in _PROVIDER_SCENE_FLAGS
    }
)


# Params that name an *existing* object a mutating command touches, so the transaction
# can capture that object's state and restore it on failure. "name" is excluded: in
# create_primitive it names a new object, not one to protect.
_TARGET_NAME_PARAMS: tuple[str, ...] = (
    "object_name",
    "camera_name",
    "light_name",
    "curve_object_name",
    "cutter_object_name",
    "reference_object_name",
    "target_object_name",
    "cloth_object_name",
    "garment_object_name",
    "armature_object_name",
    "mesh_object_name",
    "source_mesh_name",
    "target_mesh_name",
    "constraint_object_name",
    "object1_name",
    "object2_name",
    "low_resolution_source_name",
    "render_object_name",
    "proxy_object_name",
    "source_object_name",
    "destination_name",
    "movement_object_name",
    "owner_name",
    "source_root_name",
    "root_object_name",
    "domain_object_name",
    "guide_object_name",
    "guide_parent_domain_object_name",
    "instance_object_name",
)
_TARGET_NAMES_PARAMS: tuple[str, ...] = (
    "object_names",
    "camera_names",
    "body_collider_object_names",
    "source_object_names",
    "collider_object_names",
    "mesh_object_names",
    "armature_object_names",
    "body_names",
    "child_object_names",
    "piece_object_names",
)

# The object-name keys the record params above nest one level down.
_RECORD_OBJECT_NAME_KEYS = (
    "object_name",
    "render_object_name",
    "proxy_object_name",
    "low_resolution_source_name",
    "convex_source_object_name",
)

# Params holding records that name objects: the container key, and the keys inside
# one record that name an object. Order matters - it is the order targets are
# captured and therefore restored in.
_TARGET_RECORD_PARAMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("targets", ("object_name",)),
    ("fields", ("object_name",)),
    ("keyframes", ("object_name",)),
    ("assignments", ("child_object_name", "parent_object_name")),
    ("constraint", ("target_object_name",)),
    ("sources", _RECORD_OBJECT_NAME_KEYS),
    ("mappings", _RECORD_OBJECT_NAME_KEYS),
    ("bodies", _RECORD_OBJECT_NAME_KEYS),
)


def _records(value: object) -> tuple[Mapping[str, object], ...]:
    """
    Read a record param as the records it holds, whether it is one or a list.

    `constraint` is a single record while `sources` is a list of them; both are
    otherwise walked identically, so normalizing here is what lets one table
    describe every nested walk.

    Args:
        value: The param's value, as the client sent it.

    Returns:
        tuple[Mapping[str, object], ...]: The records, or empty for any other shape.

    """
    if isinstance(value, Mapping):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(record for record in value if isinstance(record, Mapping))
    return ()


def target_names(params: Mapping[str, object]) -> list[str]:
    """
    Read the names of existing objects a mutating request says it will touch.

    Rollback protection is decided by parameter-naming convention, which is what
    this function is: the three tables above are the whole convention, and a
    mutating command that spells an object parameter some other way gets no
    rollback. Pure, and free of `bpy`, so the convention can be tested without a
    database; `BlenderMCPServer._resolve_targets` turns these names into objects.

    Args:
        params: The command's params, as the client sent them.

    Returns:
        list[str]: The named objects, duplicates collapsed, first mention first.

    """
    names: list[str] = []
    for key in _TARGET_NAME_PARAMS:
        value = params.get(key)
        if isinstance(value, str):
            names.append(value)
    for key in _TARGET_NAMES_PARAMS:
        value = params.get(key)
        if isinstance(value, (list, tuple)):
            names.extend(name for name in value if isinstance(name, str))
    for container_key, name_keys in _TARGET_RECORD_PARAMS:
        for record in _records(params.get(container_key)):
            names.extend(record[key] for key in name_keys if isinstance(record.get(key), str))
    return list(dict.fromkeys(names))


class CommandRegistryMixin:
    """Answer, for the server, what the registry says about one command or the whole table."""

    @staticmethod
    def command_spec(cmd_type: object) -> CommandSpec:
        """
        Read one command's row out of the registry.

        An unregistered name gets `_UNCLASSIFIED`, whose every answer is the safe
        one, so a classification question about a command this add-on cannot
        dispatch can never be answered "skip the transaction".

        Args:
            cmd_type: The command name from the frame; any type, since it arrives
                from a client.

        Returns:
            CommandSpec: That command's classification.

        """
        return COMMANDS.get(cmd_type, _UNCLASSIFIED) if isinstance(cmd_type, str) else _UNCLASSIFIED

    def is_read_only_command(self, cmd_type: str, params: Mapping[str, object]) -> bool:
        """
        Report whether this call reads bpy.data without changing it.

        Two fields of the command's spec answer it: `read_only`, for commands that
        never mutate, and `read_only_when`, for the ones whose params decide (an
        `INSPECT` cache call, a dry-run sewing preview, a conformity analysis asked
        for no heat map). Pure: it reads the registry and the params.

        Args:
            cmd_type: The MCP command type.
            params: The command's params, as the client sent them.

        Returns:
            bool: True when the call mutates nothing.

        """
        spec = self.command_spec(cmd_type)
        return spec.read_only or (spec.read_only_when is not None and spec.read_only_when(params))

    def bypasses_transaction(self, cmd_type: str, params: Mapping[str, object]) -> bool:
        """
        Report whether this call must run outside `mutation_transaction`.

        Read-only and non-undo calls skip it because a snapshot, a diff and an
        undo checkpoint would buy nothing. Swaps are not read-only, but a
        transaction cannot describe them: after a load every id looks new, and
        a rollback would remove the whole file. The library commands skip it
        for the same reason; `unlink_libraries` fires no handler, so this
        routing is its only protection.

        Args:
            cmd_type: The MCP command type.
            params: The command's params, as the client sent them.

        Returns:
            bool: True when the handler runs unwrapped.

        """
        spec = self.command_spec(cmd_type)
        return (
            self.is_read_only_command(cmd_type, params)
            or spec.non_undo
            or (spec.non_undo_when is not None and spec.non_undo_when(params))
            or spec.session_swap
            or spec.datablock_replacing
        )

    def _provider_gates(self) -> tuple[bool, ...]:
        """
        Read the open .blend's provider flags, in `_PROVIDER_SCENE_FLAGS` order.

        Returns:
            tuple[bool, ...]: One flag per provider; also the key the handler map
            is memoized under, since it is the only thing the map varies with.

        """
        scene = bpy.context.scene
        return tuple(bool(getattr(scene, flag, False)) for flag in _PROVIDER_SCENE_FLAGS.values())

    def _build_command_handlers(self) -> Mapping[str, Callable[..., object]]:
        """
        Map every dispatchable command name to this server's bound handler.

        Shared by execute_command_internal (dispatch) and get_addon_info
        (advertised capabilities), so the two can never drift apart. A name is
        also its handler's attribute name, so `COMMANDS` cannot list a command
        this class does not implement without `getattr` saying so at once.

        Memoized per provider-flag combination, because that combination is the
        only thing the map varies with. Rebuilding ~300 bound methods used to be
        paid twice per command - once to dispatch it and once for the swap
        barrier's `_is_dispatchable` - on Blender's main thread, which is the
        latency `_poll_interval` exists to cut.

        Returns:
            Mapping[str, Callable[..., object]]: Command name to bound handler,
            shared and memoized, so a caller must not mutate it.

        """
        gate = self._provider_gates()
        handlers = self._handlers_by_gate.get(gate)
        if handlers is None:
            enabled_names = (
                _GATED_COMMAND_NAMES[provider] for provider, on in zip(_PROVIDER_SCENE_FLAGS, gate, strict=True) if on
            )
            handlers = {name: getattr(self, name) for name in itertools.chain(_UNGATED_COMMAND_NAMES, *enabled_names)}
            self._handlers_by_gate[gate] = handlers
        return handlers

    def _is_dispatchable(self, cmd_type: object) -> bool:
        """
        Report whether a command name has a handler behind it right now.

        Resolves the whole handler map, which is memoized, so this is a dict lookup.

        Args:
            cmd_type: The command name from the frame.

        Returns:
            bool: True when `execute_command` would find a handler for it.

        """
        return cmd_type in self._build_command_handlers()
