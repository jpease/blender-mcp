import functools
import itertools
import json
import logging
import os
import queue
import socket
import tempfile
import threading
import time

from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

import bpy
import mathutils

from . import ADDON_PROTOCOL_VERSION, authored, bl_info
from .capability_introspection import capability_params
from .file_paths import canonical_path
from .handlers.animation import AnimationHandlersMixin
from .handlers.camera import CameraHandlersMixin
from .handlers.character_rigging import CharacterRiggingHandlersMixin
from .handlers.cloth import ClothHandlersMixin
from .handlers.delivery import DeliveryHandlersMixin
from .handlers.file_lifecycle import FileLifecycleHandlersMixin
from .handlers.lighting import LightingHandlers
from .handlers.linking import LinkingHandlersMixin
from .handlers.liquid import LiquidHandlersMixin
from .handlers.mesh import MeshHandlersMixin
from .handlers.model import ModelHandlersMixin
from .handlers.nd import NDHandlersMixin
from .handlers.object_animation import ObjectAnimationHandlersMixin
from .handlers.polyhaven import PolyhavenHandlersMixin
from .handlers.rendering import RenderingHandlersMixin
from .handlers.retopology import RetopologyHandlersMixin
from .handlers.rigid_body import RigidBodyHandlersMixin
from .handlers.scene import SceneHandlersMixin
from .handlers.scene_physics import ScenePhysicsHandlersMixin
from .handlers.sketchfab import SketchfabHandlersMixin
from .handlers.viewport import ViewportHandlersMixin
from .helpers import get_blendermcp_addon_preferences, get_mesh_object, paginate, sync_from_editmode
from .object_lookup import find_object
from .output_roots import configured_file_roots, configured_roots, writable_roots
from .session import load_in_flight, mark_session_indeterminate, session_is_indeterminate, session_snapshot
from .text_hygiene import client_safe_name_leaf
from .transaction import mutation_transaction, unreferenced_warning

# The add-on's own logger. Blender installs no handler for it, so by default these
# records reach the console through the root logger exactly as the `print` calls they
# replaced did - but a level now separates one dispatched command from a dropped
# connection, and an operator can silence or raise either without editing the add-on.
logger = logging.getLogger(__name__)

# The optional integrations whose commands are gated on a per-.blend scene flag.
Provider = Literal["polyhaven", "sketchfab", "nd"]


@functools.lru_cache(maxsize=1)
def _canonical_file_roots(roots: tuple[str, ...]) -> tuple[str, ...]:
    """
    Canonicalize the configured file roots, once per distinct configuration.

    Memoized because `realpath` stats every path component, on Blender's main
    thread, during each handshake. A root that does not exist is kept: dropping
    it would turn a misconfigured enforced deployment into an unenforced one.

    Args:
        roots: The configured roots, in order; a tuple so it can key the cache.

    Returns:
        tuple[str, ...]: Canonical roots, deduplicated, order kept.

    """
    return tuple(dict.fromkeys(canonical_path(root) for root in roots))


@functools.lru_cache(maxsize=1)
def _probe_writable_roots(candidates: tuple[str | None, ...]) -> tuple[str, ...]:
    """
    Probe candidate roots for writability, once per distinct candidate list.

    Every MCP connection's handshake reaches this on Blender's main thread, and
    a stat on a hung network mount blocks there, freezing the UI and the command
    queue. The drain time budget cannot help: it is checked only between
    commands. Keying on the list, rather than caching outright, re-probes when
    the open .blend changes.

    Args:
        candidates: Paths to consider, most preferred first; a tuple so it can
            key the cache.

    Returns:
        tuple[str, ...]: Absolute writable directories, preference order kept.
        Immutable so a caller cannot change the cached answer.

    """
    return tuple(writable_roots(candidates))


class HandlerReportedError(Exception):
    """
    A handler reported failure by *returning* a failure shape instead of
    raising (e.g. {"error": ...} from a provider import).

    Raised inside the mutation transaction so a partially-applied request rolls
    back like any other failure, instead of committing the partial state and
    pushing an undo checkpoint. Its message is what the client receives.
    """


def _handler_failure_message(result):
    """
    Detect a handler that returned a failure shape rather than raising.

    Deliberately mirrors connection.ad_hoc_failure_message on the server side;
    the two live in separate runtimes (the addon must not import server code,
    and vice versa), so the shared shape contract is duplicated on purpose -
    keep the two in sync. A {"cancelled": True} outcome (ND operators the user
    cancelled) is NOT a failure and must fall through.

    Args:
        result: The value a handler returned.

    Returns:
        str | None: The failure message if `result` is a known failure shape,
        else None.

    """
    if isinstance(result, dict):
        if result.get("cancelled"):
            return None
        if result.get("succeed") is False:
            return str(result.get("error") or result)
        error = result.get("error")
        if error:
            return str(error)
    return None


def extract_frames(buffer: bytes, max_message_bytes: int) -> tuple[list[bytes], bytes, bool]:
    r"""
    Split a receive buffer into the complete `\n`-delimited frames it holds.

    A single json.loads() over the whole buffer cannot tell "incomplete
    message" apart from "complete message plus the start of the next one":
    both raise json.JSONDecodeError, and treating the second as "wait for
    more" means the buffer can never parse again, since the trailing bytes are
    never valid on their own. Splitting on the newline terminator each side
    appends removes the ambiguity - each complete line is exactly one message.
    An empty line is a stray terminator, not a frame, and is skipped.

    Both size rules live here, because both are the same protocol violation
    seen at different moments: a frame larger than the cap, and a remainder
    larger than it with no terminator in sight. Without the second, a client
    that never terminates a message would grow the buffer forever.

    Scans each byte once (`find` from where the last frame ended, rather than
    re-splitting the tail per frame), so a pipelined burst costs one pass.

    Args:
        buffer: Everything received on this connection and not yet consumed.
        max_message_bytes: The largest single frame, and the largest
            terminator-less remainder, this server accepts.

    Returns:
        tuple[list[bytes], bytes, bool]: The complete frames, terminator
        excluded; the bytes left for the next `recv`; and whether the
        connection must be dropped. Frames that arrived intact before an
        oversized one are still returned - they are worth answering.

    """
    frames: list[bytes] = []
    start = 0
    while True:
        end = buffer.find(b"\n", start)
        if end < 0:
            break
        line = buffer[start:end]
        start = end + 1
        if not line:
            continue
        if len(line) > max_message_bytes:
            return frames, buffer[start:], True
        frames.append(line)
    return frames, buffer[start:], len(buffer) - start > max_message_bytes


def parse_command_frame(line: bytes) -> tuple[dict[str, object] | None, str | None, str | None]:
    """
    Decide whether one frame is a command this server can queue.

    Transport shape only - `id`, `type` and `params` - which is all this layer
    can judge: it runs on a client thread, where reading bpy data is not
    allowed. Any well-shaped frame is queued and judged further by the main
    thread.

    Pure: the caller owns the socket write, the session stamp and the queue.

    Args:
        line: One frame's bytes, terminator excluded.

    Returns:
        tuple[dict | None, str | None, str | None]: The command, or None when
        the frame is not one; the id to answer under, or None when the frame
        never produced a usable one; and a client-safe protocol error, or None
        when the frame is acceptable.

    """
    try:
        command = json.loads(line.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None, "Malformed UTF-8 JSON command"

    if not isinstance(command, dict):
        return None, None, "Command must be a JSON object"
    request_id = command.get("id")
    if request_id is not None and not isinstance(request_id, str):
        # No id to answer under: a non-string one cannot be echoed back as the
        # protocol's `id`, and inventing one would match nothing on the client.
        return None, None, "Command id must be a string when provided"
    if not isinstance(command.get("type"), str) or not command["type"].strip():
        return None, request_id, "Command type must be a non-empty string"
    if not isinstance(command.get("params", {}), dict):
        return None, request_id, "Command params must be a JSON object"
    return command, request_id, None


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
        "evaluate_procedural_geometry": CommandSpec(read_only=True),
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


@dataclass(slots=True)
class AnswerReceipt:
    """
    Who owes one command's client its single response frame.

    `_execute_and_answer` and `_answer` are the only writers and `_run_session_swap`
    the only reader, but the question outlives the call that answers it: on an abort
    the swap has to know whether anything already began writing, because a second
    frame desyncs a client that matches responses by order and no frame at all costs
    it a timeout.

    Attributes:
        answered: True once `_answer` began writing, whether or not the write
            finished.

    """

    answered: bool = False


class BlenderMCPServer(
    ViewportHandlersMixin,
    AnimationHandlersMixin,
    CameraHandlersMixin,
    LightingHandlers,
    CharacterRiggingHandlersMixin,
    RigidBodyHandlersMixin,
    RetopologyHandlersMixin,
    MeshHandlersMixin,
    ModelHandlersMixin,
    ClothHandlersMixin,
    LiquidHandlersMixin,
    FileLifecycleHandlersMixin,
    DeliveryHandlersMixin,
    LinkingHandlersMixin,
    SceneHandlersMixin,
    ScenePhysicsHandlersMixin,
    ObjectAnimationHandlersMixin,
    RenderingHandlersMixin,
    NDHandlersMixin,
    PolyhavenHandlersMixin,
    SketchfabHandlersMixin,
):
    """Serve MCP commands from clients through the Blender addon."""

    def __init__(self, host="localhost", port=9876) -> None:
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None
        # Commands are pushed here by client threads and drained by a single
        # timer running on Blender's main thread. bpy.app.timers is not
        # thread-safe, so registering a timer per command (the previous
        # approach) could silently drop the callback - on Windows especially -
        # leaving the client blocked in recv() until its socket timeout.
        self.command_queue = queue.Queue(maxsize=self._MAX_QUEUED_COMMANDS)
        # Client socket -> its write lock. stop() shuts the sockets down to free
        # threads blocked in recv(); the lock keeps two writers' frames apart.
        self._clients = {}
        self._clients_lock = threading.Lock()
        # The exact object registered with bpy.app.timers; see _register_drain_timer.
        self._drain_timer = None
        # When the last command was taken off the queue, for _poll_interval below.
        self._last_command_at = 0.0
        # Bound handler maps, one per distinct provider-flag combination; see
        # `_build_command_handlers`. Per instance, because the values are this
        # server's bound methods.
        self._handlers_by_gate: dict[tuple[bool, ...], Mapping[str, Callable[..., object]]] = {}

    def _get_config_value(self, scene_attr, pref_attr=None, env_var=None):
        """
        Read config in order: addon preferences -> scene -> env var.

        Args:
            scene_attr: Value for scene attr.
            pref_attr: Value for pref attr.
            env_var: Value for env var.

        Returns:
            Result produced by the operation.

        """
        prefs = get_blendermcp_addon_preferences()
        if prefs and pref_attr:
            pref_value = getattr(prefs, pref_attr, "")
            if pref_value:
                return pref_value

        scene_value = getattr(bpy.context.scene, scene_attr, "")
        if scene_value:
            return scene_value

        if env_var:
            env_value = os.getenv(env_var, "")
            if env_value:
                return env_value
        return ""

    def get_sketchfab_api_key(self):
        return self._get_config_value(
            "blendermcp_sketchfab_api_key",
            "sketchfab_api_key",
            "BLENDERMCP_SKETCHFAB_API_KEY",
        )

    def start(self) -> None:
        if bpy.app.background:
            logger.error(
                "Cannot start the server in background mode (blender -b) - commands would never execute. "
                "Run Blender with a GUI, or use a virtual display: xvfb-run -a blender"
            )
            return

        if self.running:
            logger.warning("Server is already running")
            return

        self.running = True

        try:
            # Create socket
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            # Backlog of 1 meant a reconnecting client could complete the TCP
            # handshake and then never be accept()ed - a connection that looks
            # established but is never serviced.
            self.socket.listen(5)

            # Start server thread
            self.server_thread = threading.Thread(target=self._server_loop)
            self.server_thread.daemon = True
            self.server_thread.start()

            # start() is called from the operator, i.e. the main thread, so
            # this is the only safe place to touch bpy.app.timers.
            self._register_drain_timer()

            logger.info("Server started on %s:%s", self.host, self.port)
        except Exception:
            logger.exception("Failed to start the server on %s:%s", self.host, self.port)
            self.stop()

    def _register_drain_timer(self) -> None:
        """
        Register the drain callback, keeping the exact object Blender was given.

        `bpy.app.timers` matches callbacks by identity, and each access to
        `self.drain_command_queue` creates a new bound method, which
        `is_registered()` and `unregister()` do not recognize.

        Main thread only: `bpy.app.timers` is not thread-safe, and a registration
        from another thread can be silently dropped.
        """
        if self._drain_timer is not None and bpy.app.timers.is_registered(self._drain_timer):
            return
        self._drain_timer = self.drain_command_queue
        bpy.app.timers.register(self._drain_timer, persistent=True)

    def _unregister_drain_timer(self) -> None:
        """
        Remove the drain callback, so restarts cannot pile up timers.

        Each extra timer brings its own per-tick command and time allowance,
        multiplying the main-thread load those limits exist to cap. Blender may
        already have dropped the timer, which is not an error.
        """
        timer = self._drain_timer
        self._drain_timer = None
        if timer is None:
            return
        with suppress(Exception):
            if bpy.app.timers.is_registered(timer):
                bpy.app.timers.unregister(timer)

    def stop(self) -> None:
        self.running = False
        self._unregister_drain_timer()

        # Close socket
        if self.socket:
            with suppress(Exception):
                self.socket.close()
            self.socket = None

        # Shut down live client sockets. Without this, handler threads stay
        # parked in a blocking recv() forever; being daemon threads they then
        # outlive the restart and close connections the new server owns
        # (the WinError 10054 seen after toggling the addon).
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            with suppress(Exception):
                client.shutdown(socket.SHUT_RDWR)
            with suppress(Exception):
                client.close()

        # Drop any commands that will never be serviced now.
        while True:
            try:
                self.command_queue.get_nowait()
            except queue.Empty:
                break

        # Wait for thread to finish
        if self.server_thread:
            try:
                if self.server_thread.is_alive():
                    self.server_thread.join(timeout=1.0)
            except Exception:
                pass
            self.server_thread = None

        logger.info("Server stopped")

    def _server_loop(self) -> None:
        """Main server loop in a separate thread."""
        logger.info("Server thread started")
        self.socket.settimeout(1.0)  # Timeout to allow for stopping

        while self.running:
            try:
                # Accept new connection
                try:
                    client, address = self.socket.accept()
                    logger.info("Connected to client: %s", address)

                    # Handle client in a separate thread
                    client_thread = threading.Thread(target=self.handle_client, args=(client,))
                    client_thread.daemon = True
                    client_thread.start()
                except TimeoutError:
                    # Just check running condition
                    continue
                except Exception:
                    logger.exception("Could not accept a connection; retrying")
                    time.sleep(0.5)
            except Exception:
                logger.exception("Server loop error")
                if not self.running:
                    break
                time.sleep(0.5)

        logger.info("Server thread stopped")

    def drain_command_queue(self) -> float | None:
        """
        Run queued commands on Blender's main thread.

        Registered once by start(); returns the poll interval so Blender keeps
        calling it. All bpy access happens here, on the main thread.

        Two checks keep a command from running against a file it was not sent
        for, and each covers the other's gap. The queue snapshot in
        `_run_session_swap` rejects what was queued before a swap, but not what
        arrives during the load. The stamp from `_stamp_session` rejects those,
        but not commands queued before a failed swap, because a failed load
        leaves the epoch alone. The stamp is checked first, so a stale swap
        command cannot trip the barrier again.

        Returns:
            Result produced by the operation.

        """
        if not self.running:
            return None

        try:
            self._drain_batch()
        except BaseException:
            # Blender unregisters a timer callback that raises, even a persistent
            # one, but an abort must still propagate. Without a successor timer
            # the drain loop ends and every client waits out its 180 s timeout.
            self._replace_this_dying_timer()
            raise
        return self._poll_interval()

    def _poll_interval(self) -> float:
        """
        Say how long Blender should wait before draining the queue again.

        `bpy.app.timers` is the only thread-safe way to reach Blender's main thread from a
        socket thread, so this interval is the latency floor under every command: a client
        that sends its next command immediately after reading a reply always misses the
        tick that just ran and waits out a whole interval. At a flat 50 ms that measured as
        54.86 ms of pure protocol cost per frame on a 240-frame orchestrated animation -
        13.2 s a shot, for renders whose frames took 2.5 ms each
        (`scripts/rig_scenarios/scenario_render_overhead.py`).

        So the poll follows the traffic: fast while a session is active, and back to the
        cheap idle rate once it goes quiet, which is what keeps an idle Blender idle. The
        window is generous because the gap between two commands of one sequence includes
        whatever Blender spent on the first - a slow frame must not drop the session back
        to the idle rate before its successor arrives.

        Returns:
            float: Seconds until the next drain.

        """
        if time.monotonic() - self._last_command_at < self._ACTIVE_WINDOW_SECONDS:
            return self._ACTIVE_POLL_SECONDS
        return self._IDLE_POLL_SECONDS

    def _drain_batch(self) -> None:
        """
        Run up to one tick's worth of queued commands, applying both barriers.

        A separate method so the recovery in `drain_command_queue` covers all of
        it, including the first `get_nowait`.
        """
        processed = 0
        deadline = time.monotonic() + self._DRAIN_TIME_BUDGET_SECONDS
        while processed < self._MAX_COMMANDS_PER_TICK and time.monotonic() < deadline:
            try:
                command, client = self.command_queue.get_nowait()
            except queue.Empty:
                break

            # Popped so handlers never see it.
            stamp = command.pop(self._SESSION_STAMP_KEY, None)
            refusal = self._refusal_reason(command, stamp)
            if refusal is not None:
                self._discard_superseded([(command, client)], refusal)
                processed += 1
                continue

            # Only a swap the addon can run may discard the queue, which holds
            # other server processes' commands; any other name falls through and
            # is answered "Unknown command type". The command is already off the
            # queue, so a failure to classify it must be answered here.
            try:
                is_swap = self.command_spec(command.get("type")).session_swap and self._is_dispatchable(
                    command.get("type")
                )
            except Exception:
                logger.exception("Could not classify a dequeued command; answering its client instead")
                self._answer(command, client, {"status": "error", "message": "Command could not be dispatched"})
                processed += 1
                continue

            if is_swap:
                self._run_session_swap(command, client)
                break

            self._execute_and_answer(command, client)
            processed += 1
            if self.command_spec(command.get("type")).tick_ending:
                break
        if processed:
            self._last_command_at = time.monotonic()

    def _replace_this_dying_timer(self) -> None:
        """
        Hand the drain loop to a fresh callback before Blender drops this one.

        Registering is safe here because timer callbacks run on the main thread.
        Clearing `_drain_timer` stops `_register_drain_timer` returning early, and
        the bound method it registers is a new object, distinct from the dying
        one. A stopped server gets no successor, so this cannot undo `stop()`.

        The dying timer is unregistered explicitly, so exactly one drain timer
        remains even where raising callbacks are not dropped, as in the test
        stub. A failed handoff is logged, because clients would otherwise time
        out with nothing in the console to say why.
        """
        if not self.running:
            return
        dying = self._drain_timer
        self._drain_timer = None
        if dying is not None:
            with suppress(Exception):
                bpy.app.timers.unregister(dying)
        try:
            self._register_drain_timer()
        except Exception:
            logger.exception("Could not hand the drain loop to a fresh timer - restart the MCP server")

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

    @staticmethod
    def _session_marker() -> tuple[object, object]:
        """
        Read the pair that identifies the open database.

        The epoch restarts at 0 when the addon's modules reload, so a client can
        see the same epoch for a different database. `session_id` is new on
        every reload, which makes the pair unique.

        Returns:
            tuple: `(session_id, session_epoch)`, both immutable.

        """
        snapshot = session_snapshot()
        return (snapshot["session_id"], snapshot["session_epoch"])

    def _stamp_session(self, command: dict) -> None:
        """
        Record, on the client thread, which database a command was queued against.

        Nothing in the queue shows whether a command arrived before, during or
        after a load, so the marker is taken at enqueue and compared at dequeue.
        The pre-swap queue snapshot cannot do this: the epoch moves only when a
        load ends, and client threads keep queuing throughout it.

        Safe off the main thread because it reads module state, not `bpy`.

        Args:
            command: The decoded command, mutated in place. The key is
                overwritten, so a client cannot forge it.

        """
        command[self._SESSION_STAMP_KEY] = self._session_marker()

    def _execute_and_answer(
        self,
        command: dict,
        client: object,
        *,
        receipt: "AnswerReceipt | None" = None,
    ) -> None:
        """
        Run one command and write exactly one response frame back to its client.

        Catches `BaseException` because an abort such as `KeyboardInterrupt`
        lands wherever the main thread is, including inside a long file load.
        The client is answered first and the abort re-raised, so Blender still
        sees it.

        Args:
            command: The decoded command to run.
            client: The socket its response belongs on.
            receipt: Optional receipt `_answer` marks as it starts, so
                `_run_session_swap` can tell whether it still owes the client an
                answer.

        """
        response: dict
        try:
            response = self.execute_command(command)
        except Exception as e:
            logger.exception("Command %s failed before it produced a response", command.get("type"))
            response = {"status": "error", "message": str(e)}
        except BaseException as e:
            logger.error("Command aborted by %s: answering the client before re-raising", type(e).__name__)
            self._answer(
                command,
                client,
                {
                    "status": "error",
                    "message": (
                        f"Blender aborted this command ({type(e).__name__}) before it produced a result; "
                        "the session may be in an indeterminate state - poll get_session_info before resending."
                    ),
                },
                receipt=receipt,
            )
            raise

        self._answer(command, client, response, receipt=receipt)

    def _answer(self, command: dict, client: object, response: dict, *, receipt: "AnswerReceipt | None" = None) -> None:
        """
        Write one response frame, whatever the handler did or did not produce.

        Every response carries the session marker, so the MCP server notices a
        file opened from Blender's own UI and re-reads its cached handshake. The
        fallback error frames from `_encode_response` carry no marker.

        The receipt is written before anything that can fail, so it records an
        answer begun, not finished. An abort mid-send can then leave the client
        with no frame, which costs a timeout; recording later could send two,
        which desyncs a client that matches responses by order.

        Args:
            command: The command being answered, for its echoed id.
            client: The socket the frame belongs on.
            response: The response body.
            receipt: Optional receipt to record the answer in; see above.

        """
        if receipt is not None:
            receipt.answered = True

        # Echo the id so the client can match responses without relying on order.
        response["id"] = command.get("id")
        response["session_id"], response["session_epoch"] = self._session_marker()

        # Outside the try, so an encoding failure is never reported as a disconnect.
        payload = self._encode_response(response, command.get("id"))
        try:
            self._send_frame(client, payload)
        except Exception:
            logger.warning("Failed to send a response frame - the client disconnected")

    def _drain_queue_into(self, superseded: list[tuple[dict, object]]) -> None:
        """
        Empty the command queue into a list the caller already owns.

        Appending, rather than returning a list, means an abort part-way through
        loses nothing: the caller still answers every command already taken.
        The loop has its own bound because client threads can refill the queue
        as fast as it empties.

        Args:
            superseded: The list to append `(command, client)` pairs to.

        """
        for _slot in range(self._MAX_QUEUED_COMMANDS):
            try:
                superseded.append(self.command_queue.get_nowait())
            except queue.Empty:
                break

    def _run_session_swap(self, command: dict, client: object) -> None:
        """
        Run a command that replaces the database, and discard the batch behind it.

        The queue is emptied before the swap runs, so everything discarded was
        queued against the old file. Rejections go out in the `finally`, after
        the swap, so they name the epoch that holds once the outcome is known. A
        swap its own validation refuses still discards the queue; fixing that
        means moving validation out of the handler.

        On an abort, only `load_in_flight()` decides whether the database may
        now mix two files. The `try` also spans the queue drain, and inferring a
        load from what did not happen can latch falsely or miss a real abort. If
        a load was in flight, the session is marked indeterminate, moving the
        epoch before the `finally` sends rejections: no load handler fires on an
        abort, so commands stamped during the load would otherwise still match.
        The swap's own client is answered here only if the receipt shows nobody
        began answering it.

        Args:
            command: The session-swap command to run.
            client: The socket its response belongs on.

        """
        superseded: list[tuple[dict, object]] = []
        receipt = AnswerReceipt()

        try:
            self._drain_queue_into(superseded)
            self._execute_and_answer(command, client, receipt=receipt)
        except BaseException:
            # Both `if`s can apply at once, so neither may become an `elif`. Read
            # the flag before the latch clears it; the message depends on it.
            mid_load = load_in_flight()
            if mid_load:
                mark_session_indeterminate()
            if not receipt.answered:
                self._answer(command, client, {"status": "error", "message": self._abort_message(mid_load)})
            raise
        finally:
            self._discard_superseded(superseded)

    # Sent to the swap's own client when nothing began answering it. It says the
    # database is unchanged, so it is used only when no load is in flight; an
    # unwritten receipt alone does not prove that. It names no path, because
    # this socket is unauthenticated.
    _ABORTED_BEFORE_HANDOFF = (
        "Blender aborted before this file swap was dispatched; nothing was loaded and the open "
        "database is unchanged. Poll get_session_info, then resend."
    )
    # The same case with a load in flight. It asks for a known shot, not a
    # resend, because the database may now mix two files.
    _ABORTED_MID_LOAD = (
        "Blender aborted this file swap while a load was in flight; the open database may be part of "
        "two files and this session is now marked indeterminate. Poll get_session_info and open a "
        "known shot before resending anything."
    )
    # Why a dequeued command was answered without running. An unstamped command
    # gets its own reason because no swap was involved.
    _SUPERSEDED_REASON = (
        "a session file swap was attempted while this command was queued, "
        "so it was never run against the file it was sent for"
    )
    _UNSTAMPED_REASON = (
        "it reached the queue without the session stamp the enqueue path applies, "
        "so which database it was sent for cannot be established"
    )
    # Unlike the reasons above, this lasts until a load completes, so a resend
    # would be refused again; the client must open a known shot instead.
    _INDETERMINATE_REASON = (
        "a session file swap was aborted part-way and no load has completed since, "
        "so the open database may be part of two files; poll get_session_info and "
        "open a known shot before resending anything"
    )

    def _abort_message(self, mid_load: bool) -> str:
        """
        Pick the abort message by whether a load was in flight.

        Args:
            mid_load: Whether a load was in flight when the abort was observed.

        Returns:
            str: The message for that case.

        """
        return self._ABORTED_MID_LOAD if mid_load else self._ABORTED_BEFORE_HANDOFF

    def _refusal_reason(self, command: Mapping[str, object], stamp: object) -> str | None:
        """
        Say why a dequeued command must be answered instead of run, or that it may run.

        The stamp is checked first, so a command queued before a failed swap is
        reported as stale rather than tripping the indeterminate barrier again. A
        missing stamp is its own refusal: the only enqueue path always stamps, so
        only a bug gets here. The indeterminate check comes after, so a stale
        command is still reported as stale, and before dispatch, so nothing runs
        on a database that may mix two files.

        Mutates nothing: it reads the command, the stamp and the session module.

        Args:
            command: The dequeued command, its stamp already popped off.
            stamp: The session marker popped off it, or None when it carried none.

        Returns:
            str | None: The clause `_discard_superseded` folds into its message,
            or None when the command may run.

        """
        if stamp != self._session_marker():
            return self._UNSTAMPED_REASON if stamp is None else self._SUPERSEDED_REASON
        if session_is_indeterminate() and not self.command_spec(command.get("type")).indeterminate_safe:
            return self._INDETERMINATE_REASON
        return None

    def _discard_superseded(self, superseded: list[tuple[dict, object]], reason: str | None = None) -> None:
        """
        Answer every command a swap invalidated, under a deadline, so no client waits.

        The message gives the current epoch without claiming it moved. After a
        successful swap it differs from the client's cached epoch, so the client
        re-handshakes; after a failed swap it does not, and the client can simply
        resend. It names no path, because this layer must not hand a client an
        absolute path.

        Every peer gets a send attempt; once the deadline passes, each attempt
        just waits less. Skipping peers instead would drop healthy connections,
        and this frame is their only prompt to re-handshake. A peer is closed
        after its first failed write, because a timed-out `sendall` may have
        written part of a frame and another write would break the newline
        framing. Closing turns the partial line into an EOF the client can act on.

        Worst case on the main thread is about 1.26 s: the 0.25 s budget, one
        send still in flight (lock and write, 0.25 s each), then 2 ms for each of
        up to `_MAX_QUEUED_COMMANDS` entries past the deadline.

        Args:
            superseded: `(command, client)` pairs taken off the queue, bounded
                by `_drain_queue_into`.
            reason: Why these commands were not run; defaults to the swap case.

        """
        snapshot = session_snapshot()
        epoch = snapshot["session_epoch"]
        message = (
            f"Discarded without running: {reason or self._SUPERSEDED_REASON}. "
            f"The session epoch is now {epoch} - "
            "re-handshake first if that differs from the epoch you last saw, then resend."
        )
        deadline = time.monotonic() + self._REJECTION_TIME_BUDGET_SECONDS
        abandoned: set[object] = set()
        for command, client in superseded:
            # Closed earlier in this pass, so a write could only fail.
            if client in abandoned:
                continue
            frame = self._encode_frame(
                {
                    "id": command.get("id"),
                    "status": "error",
                    "message": message,
                    "session_id": snapshot["session_id"],
                    "session_epoch": epoch,
                }
            )
            timeout = (
                self._REJECTION_SEND_TIMEOUT_SECONDS
                if time.monotonic() < deadline
                else self._PAST_BUDGET_SEND_TIMEOUT_SECONDS
            )
            if not self._send_bounded(client, frame, timeout):
                self._abandon_unreachable_client(client)
                abandoned.add(client)

    def _send_bounded(self, client: object, payload: bytes, timeout: float) -> bool:
        """
        Write one frame under a caller-chosen deadline, then put the socket's own back.

        The socket's `handle_client` thread is blocked in `recv()` on it at the
        same time, and a short timeout left behind would make that loop spin.
        Never pass 0, which makes the socket non-blocking. The restore is in a
        `finally` because this runs on Blender's main thread, where Esc raises
        KeyboardInterrupt: an `except Exception` would step over that and leave
        a 1 ms timeout behind, spinning that client's `recv()` at ~1 kHz for
        the rest of the connection. If the timeout cannot be put back, the send
        counts as failed, so the caller closes the socket.

        Args:
            client: The socket to write to.
            payload: The framed bytes.
            timeout: Seconds the write may block, applied to the write lock and
                to `sendall` separately.

        Returns:
            bool: True when the frame was written and the socket's own timeout
            restored; False otherwise.

        """
        settimeout = getattr(client, "settimeout", None)
        try:
            if settimeout is not None:
                settimeout(timeout)
            self._send_frame(client, payload, timeout)
        except Exception:
            delivered = False
        else:
            delivered = True
        finally:
            # Not a `return` in the `finally`: that would swallow the
            # KeyboardInterrupt this block exists to survive.
            restored = self._restore_socket_timeout(settimeout)
        return delivered and restored

    def _restore_socket_timeout(self, settimeout: Callable[[float], object] | None) -> bool:
        """
        Put a client socket's own timeout back after a bounded write.

        Args:
            settimeout: The socket's `settimeout`, or None when it has none
                (a stub in the tests), in which case there is nothing to undo.

        Returns:
            bool: True when the socket is back on its own timeout.

        """
        if settimeout is None:
            return True
        try:
            settimeout(self._CLIENT_SOCKET_TIMEOUT_SECONDS)
        except Exception:
            logger.warning(
                "Could not restore a client socket's own timeout - closing it rather than leaving it spinning"
            )
            return False
        return True

    def _abandon_unreachable_client(self, client: object) -> None:
        """
        Close a peer this thread could not answer, so it sees EOF rather than silence.

        A silent peer waits out its full 180 s timeout; a closed one fails at
        once, and its `handle_client` thread cleans up as on any disconnect.

        Args:
            client: The socket to drop.

        """
        with self._clients_lock:
            self._clients.pop(client, None)
        with suppress(Exception):
            client.shutdown(socket.SHUT_RDWR)
        with suppress(Exception):
            client.close()

    # Messages are newline-delimited JSON. Bound how large a single message
    # can grow before we give up on it - without this, malformed input (or
    # a client that never sends a terminator) would make `buffer` grow
    # forever. The largest legitimate payloads are paginated mesh/element
    # dumps (capped well under 1000 elements); screenshots are written to
    # disk and never cross the socket. 64 MiB is generous headroom above that.
    _MAX_MESSAGE_BYTES = 64 * 1024 * 1024
    _MAX_QUEUED_COMMANDS = 256
    _MAX_COMMANDS_PER_TICK = 8
    _DRAIN_TIME_BUDGET_SECONDS = 0.02

    # The drain timer's two rates; `_poll_interval` chooses between them. 5 ms is the
    # floor a command pays while a session is active, down from the flat 50 ms this timer
    # used to return, and the idle rate is that old value - an unused Blender polls an
    # empty queue twenty times a second, exactly as before.
    _ACTIVE_POLL_SECONDS = 0.005
    _IDLE_POLL_SECONDS = 0.05
    _ACTIVE_WINDOW_SECONDS = 5.0

    # How often a client thread's recv() wakes to check self.running; also what
    # `_send_bounded` restores after a shorter write.
    _CLIENT_SOCKET_TIMEOUT_SECONDS = 1.0
    # Main-thread bounds for the rejection path; `_discard_superseded` adds them
    # up. The send timeout only has to catch a peer that has stopped reading: a
    # slow one, such as a client still draining a large response, must not be
    # closed and lose its queued commands.
    _REJECTION_SEND_TIMEOUT_SECONDS = 0.25
    _REJECTION_TIME_BUDGET_SECONDS = 0.25
    # Past the budget a frame is still attempted, with this brief timeout. It
    # must stay above zero: `settimeout(0)` makes the socket non-blocking, and
    # the `recv()` running on it in `handle_client` would raise `BlockingIOError`
    # and spin instead of timing out.
    _PAST_BUDGET_SEND_TIMEOUT_SECONDS = 0.001

    # Where `_stamp_session` puts the marker. Kept on the command dict so queue
    # items stay `(command, client)` pairs; popped before dispatch.
    _SESSION_STAMP_KEY = "_blendermcp_session_marker"

    @staticmethod
    def _encode_frame(response: dict) -> bytes:
        """
        Serialize one response as a newline-terminated frame.

        Args:
            response: The JSON-serializable response body.

        Returns:
            bytes: The encoded frame, newline terminator included.

        """
        return json.dumps(response).encode("utf-8") + b"\n"

    def _encode_response(self, response: dict, request_id: str | None) -> bytes:
        """
        Turn a handler's response into a frame that is always sendable.

        A response that cannot be serialized, such as one holding a
        `mathutils.Vector`, would otherwise send nothing and leave the client
        waiting out its timeout. The client gets a generic error and the
        traceback goes to Blender's log, since scene data, paths and
        tracebacks must not reach a client.

        Args:
            response: The response body to encode.
            request_id: The request's id, echoed back so a fallback frame stays
                matchable to the command that produced it.

        Returns:
            bytes: The encoded frame, or an error frame if `response` could not
            be serialized or exceeds `_MAX_MESSAGE_BYTES`.

        """
        try:
            payload = self._encode_frame(response)
        except (TypeError, ValueError):
            logger.exception("A response could not be serialized to JSON - sending an error frame instead")
            return self._error_frame(request_id, "Response could not be serialized to JSON")

        if len(payload) > self._MAX_MESSAGE_BYTES:
            return self._error_frame(request_id, "Response exceeded the configured message-size limit")
        return payload

    def _error_frame(self, request_id: str | None, message: str) -> bytes:
        """
        Build the one error frame shape every failure path returns.

        Args:
            request_id: The request's id, or None when it could not be read.
            message: Client-safe explanation; never a path or a traceback.

        Returns:
            bytes: The encoded error frame.

        """
        return self._encode_frame({"id": request_id, "status": "error", "message": message})

    def _send_frame(self, client, payload: bytes, lock_timeout: float | None = None) -> None:
        """
        Write one frame to a client, excluding any other writer to that socket.

        The client's own thread sends protocol errors while the main thread sends
        responses, and `sendall` is not atomic, so without the lock one frame
        could land inside another. `_clients_lock` is released before the
        per-client lock is taken, and no queue or `bpy` work happens under either.

        The rejection path passes `lock_timeout` because the other writer can
        hold the lock for as long as its own send blocks, which would stall the
        main thread past its budget. Ordinary responses wait for the lock.

        Args:
            client: The socket to write to.
            payload: The framed bytes to write.
            lock_timeout: Seconds to wait for the per-client write lock, or None
                to wait indefinitely. 0 attempts the acquisition without waiting.

        Raises:
            TimeoutError: When `lock_timeout` elapsed with the write lock still
                held by the other writer.

        """
        with self._clients_lock:
            send_lock = self._clients.get(client)
        if send_lock is None:
            # Untracked or already removed: no other writer to exclude.
            client.sendall(payload)
            return
        if lock_timeout is None:
            with send_lock:
                client.sendall(payload)
            return
        # `acquire` waits forever on -1 and rejects other negatives, so <= 0 means "don't wait".
        acquired = send_lock.acquire(False) if lock_timeout <= 0 else send_lock.acquire(timeout=lock_timeout)
        if not acquired:
            raise TimeoutError("another thread holds this client's write lock")
        try:
            client.sendall(payload)
        finally:
            send_lock.release()

    def _send_protocol_error(self, client, request_id, message) -> None:
        """
        Return a bounded transport-level validation error without touching bpy data.

        Args:
            client: The socket the offending frame arrived on.
            request_id: The request's id, or None when it could not be read.
            message: Client-safe explanation; never a path or a traceback.

        """
        with suppress(Exception):
            self._send_frame(client, self._error_frame(request_id, message))

    def _decode_and_queue_frame(self, line: bytes, client) -> None:
        r"""
        Validate one framed line and queue it as a command for the main thread.

        `parse_command_frame` makes the decision; this adds the socket write
        for a rejected frame and the queue push for an accepted one.

        Args:
            line: The bytes of one `\n`-delimited frame (never containing
                the terminator itself).
            client: The socket the frame arrived on, passed through to the
                queued command so its response goes back to the right peer.

        """
        command, request_id, error = parse_command_frame(line)
        if command is None:
            # The log gets the length the client-safe message cannot carry; the
            # payload itself stays out of it.
            logger.warning("Discarding a %d-byte frame: %s", len(line), error)
            self._send_protocol_error(client, request_id, error)
            return

        # Hand off to the main thread. Never call
        # bpy.app.timers.register() from here - it is not thread-safe and
        # the callback can be silently lost.
        #
        # Stamp at enqueue, the last point where the command's database is known.
        self._stamp_session(command)
        logger.debug("Queued command: %s", command.get("type"))
        try:
            self.command_queue.put_nowait((command, client))
        except queue.Full:
            self._send_protocol_error(client, request_id, "Blender command queue is full; retry later")

    def handle_client(self, client) -> None:
        """
        Handle connected client.

        Args:
            client: Value for client.

        """
        logger.debug("Client handler started")
        # A finite timeout keeps this loop responsive to self.running instead
        # of parking in recv() forever.
        client.settimeout(self._CLIENT_SOCKET_TIMEOUT_SECONDS)
        with self._clients_lock:
            # Shared by this thread and the main thread; see _send_frame.
            self._clients[client] = threading.Lock()
        buffer = b""

        try:
            while self.running:
                # Receive data
                try:
                    data = client.recv(8192)
                    if not data:
                        logger.info("Client disconnected")
                        break

                    frames, buffer, must_drop = extract_frames(buffer + data, self._MAX_MESSAGE_BYTES)
                    for frame in frames:
                        self._decode_and_queue_frame(frame, client)
                    if must_drop:
                        # Either a single frame or an unterminated remainder
                        # went past the cap; both are protocol violations, and
                        # the frames above arrived intact and are still queued.
                        logger.warning(
                            "Client sent a frame, or an unterminated message, over the %d-byte limit - disconnecting",
                            self._MAX_MESSAGE_BYTES,
                        )
                        break
                except TimeoutError:
                    # Expected; loop round and re-check self.running.
                    continue
                except BlockingIOError:
                    # A non-blocking socket with nothing to read, not a dead peer.
                    # This server never leaves the socket non-blocking (see
                    # `_PAST_BUDGET_SEND_TIMEOUT_SECONDS`); this branch keeps
                    # it from being fatal if something else does.
                    continue
                except Exception:
                    logger.exception("Could not receive from a client - dropping the connection")
                    break
        except Exception:
            logger.exception("Client handler failed - dropping the connection")
        finally:
            with self._clients_lock:
                self._clients.pop(client, None)
            with suppress(Exception):
                client.close()
            logger.debug("Client handler stopped")

    def execute_command(self, command):
        """
        Execute a command in the main Blender thread.

        Args:
            command: Command requested by the client.

        Returns:
            Result produced by the operation.

        """
        try:
            return self.execute_command_internal(command)
        except Exception as e:
            logger.exception("Command %s failed outside its handler", command.get("type"))
            return {"status": "error", "message": str(e)}

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

    @staticmethod
    def ping() -> dict[str, bool]:
        """
        Answer a liveness check without reading anything out of Blender.

        Deliberately free of `bpy`, and a staticmethod so it stays that way: a
        successful ping beside a failing command tells the client the transport
        and the command queue are healthy and the fault is in the data access,
        which is the only reason this command exists.

        Returns:
            dict[str, bool]: `{"pong": True}`.

        """
        return {"pong": True}

    def execute_command_internal(self, command):
        """
        Internal command execution with proper context.

        Args:
            command: Command requested by the client.

        Returns:
            Result produced by the operation.

        """
        cmd_type = command.get("type")
        params = command.get("params", {})

        handler = self._build_command_handlers().get(cmd_type)
        if handler is None:
            return {"status": "error", "message": f"Unknown command type: {cmd_type}"}
        try:
            logger.debug("Dispatching %s", cmd_type)
            return {"status": "success", "result": self._run_handler(cmd_type, handler, params)}
        except ValueError as refusal:
            # A refusal, not a fault: the handler layer raises ValueError when the client's
            # own arguments are rejected - an unconfirmed destructive call, a path outside
            # the file roots, a name that does not resolve. Printing a traceback for each one
            # teaches whoever watches Blender's console to skim past tracebacks, which is
            # where an actual fault will be. The client is told exactly the same either way.
            logger.info("Command %s refused: %s", cmd_type, refusal)
            return {"status": "error", "message": str(refusal)}
        except Exception as error:
            logger.exception("Command %s failed in its handler", cmd_type)
            return {"status": "error", "message": str(error)}

    @staticmethod
    def _resolve_targets(params):
        """
        Resolve the existing objects a mutating request will touch, from its params.

        `target_names` decides *which* names a request claims; this decides which
        of them exist. Missing objects are skipped (the handler will raise its own
        clear error). Names resolve through `find_object`, as in the handlers, so a
        rollback restores the objects the handler actually changed. An ambiguous
        name is skipped like a missing one; the handler's own lookup refuses it.

        Args:
            params: The command's params dict.

        Returns:
            list: Existing bpy objects named by the target params, first mention first.

        """
        objects = []
        for name in target_names(params):
            try:
                obj = find_object(bpy.data.objects, name)
            except ValueError:
                continue
            if obj is not None:
                objects.append(obj)
        return objects

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

    def _run_handler(self, cmd_type, handler, params):
        """
        Call a resolved handler, wrapping mutating commands in mutation_transaction.

        Args:
            cmd_type: The MCP command type, used to pick read-only vs. mutating dispatch.
            handler: The bound handler method to call.
            params: Keyword arguments to call the handler with.

        Returns:
            Result produced by the handler.

        """
        if self.bypasses_transaction(cmd_type, params):
            return handler(**params)

        targets = self._resolve_targets(params)
        with mutation_transaction(cmd_type, targets, self.command_spec(cmd_type).geometry) as txn:
            return self._finish_transaction(txn, handler(**params))

    @staticmethod
    def _finish_transaction(txn, result):
        """
        Settle a transaction the handler has already run inside, by what it returned.

        A handler that reports failure by *returning* a failure shape rather than
        raising is converted to a HandlerReportedError here, still inside the
        transaction, so its partial mutation rolls back instead of being committed
        and pushing an undo checkpoint. A cancelled outcome is neither: nothing
        happened, so nothing is checkpointed.

        Args:
            txn: The open transaction wrapping this command.
            result: What the handler returned.

        Returns:
            The handler's result, with any undo-unavailability and unreferenced-
            datablock warnings merged in.

        Raises:
            HandlerReportedError: When `result` is a failure shape.

        """
        if isinstance(result, dict) and result.get("cancelled"):
            txn.finish_without_checkpoint()
            return result
        failure = _handler_failure_message(result)
        if failure is not None:
            raise HandlerReportedError(failure)
        unreferenced = unreferenced_warning(txn.unreferenced_created())
        authored.record(txn.created_datablocks())
        notices = [note for note in (txn.commit(), unreferenced) if note]
        if notices and isinstance(result, dict):
            return {**result, "warnings": [*result.get("warnings", []), *notices]}
        return result

    def get_addon_info(self):
        """
        Version/capability handshake for the MCP server (and install tooling).

        `capabilities` depends on the open .blend's `blendermcp_use_*` flags, so
        a client re-handshakes when `session_epoch` moves; a save or a failed
        load leaves it alone. `session_indeterminate` tells a client why most
        of its commands are being refused. `capability_params` reports each
        command's accepted keyword names, or "*" for one that takes **kwargs,
        for the server's preflight parameter gate.

        Returns:
            Result produced by the operation.

        """
        session = session_snapshot()
        handlers = self._build_command_handlers()
        return {
            "name": bl_info.get("name", "Blender MCP"),
            "addon_version": list(bl_info.get("version", (0, 0))),
            "protocol_version": ADDON_PROTOCOL_VERSION,
            "capabilities": sorted(handlers),
            "capability_params": capability_params(handlers),
            "blender_version": bpy.app.version_string,
            "writable_output_roots": self._writable_output_roots(),
            # Enforced containment, unlike the advisory roots above.
            **self._file_path_policy(),
            # The pair, because the epoch restarts at 0 when the addon reloads.
            "session_id": session["session_id"],
            "session_epoch": session["session_epoch"],
            "current_filepath": session["current_filepath"],
            # Also here, so a re-handshaking client learns why it is refused.
            "session_indeterminate": session["session_indeterminate"],
        }

    @staticmethod
    def _writable_output_roots() -> list[str]:
        """
        List directories this Blender process can write renders and exports to.

        Reported because the MCP server may not share this filesystem. The list
        only ranks preferences; `_file_path_policy` publishes the enforced roots,
        which never come from these defaults because the defaults reach `~`.
        These paths are `abspath`'d, not `realpath`'d, so a symlinked root is
        listed under a different name than the one enforced. With no .blend open
        and no roots configured, `bpy.app.tempdir` ranks first, and Blender
        deletes it on exit.

        Returns:
            list[str]: Absolute, writable directories, most preferred first.
            A fresh list each call, so a caller cannot change the memoized answer.

        """
        blend_file = bpy.data.filepath
        candidates = (
            *configured_roots(),
            os.path.dirname(blend_file) if blend_file else None,
            getattr(bpy.app, "tempdir", None),
            tempfile.gettempdir(),
            os.path.expanduser("~"),
        )
        return list(_probe_writable_roots(candidates))

    @staticmethod
    def _file_path_policy() -> dict[str, object]:
        """
        Publish the roots `.blend` file commands are confined to, and whether any are.

        `file_roots_enforced` makes the unenforced default visible to clients.
        Roots are published canonical because containment compares canonical
        paths.

        Returns:
            dict[str, object]: `file_roots` (list[str]) and `file_roots_enforced` (bool).

        """
        roots = list(_canonical_file_roots(tuple(configured_file_roots())))
        return {"file_roots": roots, "file_roots_enforced": bool(roots)}

    _SCENE_INFO_MAX_LIMIT = 200

    def list_scene_objects(self, limit=25, offset=0):
        """
        Get information about the current Blender scene, paginated over its objects.

        Args:
            limit: Maximum number of items to return.
            offset: Zero-based starting position.

        Returns:
            Result produced by the operation.

        """
        try:
            scene_objects = sorted(bpy.context.scene.objects, key=lambda item: item.name.casefold())
            total = len(scene_objects)
            start, end, truncated, next_offset = paginate(total, offset, limit, self._SCENE_INFO_MAX_LIMIT)

            objects = []
            for obj in scene_objects[start:end]:
                objects.append(
                    {
                        "name": obj.name,
                        "type": obj.type,
                        # Only include basic location data
                        "location": [
                            float(obj.location.x),
                            float(obj.location.y),
                            float(obj.location.z),
                        ],
                        "parent": obj.parent.name if obj.parent else None,
                        "collections": sorted(collection.name for collection in obj.users_collection),
                        "selected": bool(obj.select_get()),
                        "visible": bool(obj.visible_get()),
                        "hide_viewport": bool(obj.hide_viewport),
                        "hide_render": bool(obj.hide_render),
                    }
                )

            scene_info = {
                "name": bpy.context.scene.name,
                "object_count": total,
                "objects": objects,
                "materials_count": len(bpy.data.materials),
                "active_object": getattr(getattr(bpy.context, "view_layer", None), "objects", None).active.name
                if getattr(getattr(getattr(bpy.context, "view_layer", None), "objects", None), "active", None)
                else None,
                "selected_objects": sorted(obj.name for obj in getattr(bpy.context, "selected_objects", ())),
                "mode": getattr(bpy.context, "mode", "UNKNOWN"),
                "unit_settings": {
                    "system": bpy.context.scene.unit_settings.system,
                    "scale_length": bpy.context.scene.unit_settings.scale_length,
                    "length_unit": bpy.context.scene.unit_settings.length_unit,
                },
                "offset": start,
                "limit": limit,
                "returned_count": len(objects),
                "truncated": truncated,
                "next_offset": next_offset,
            }

            logger.debug("Scene info collected: %d of %d objects", len(objects), total)
            return scene_info
        except Exception as e:
            logger.exception("list_scene_objects failed")
            return {"error": str(e)}

    @staticmethod
    def get_aabb(obj):
        """
        Returns the world-space axis-aligned bounding box (AABB) of an object.

        Args:
            obj: Value for obj.

        Returns:
            the world-space axis-aligned bounding box (AABB) of an object.

        Raises:
            TypeError: If the operation cannot be completed.

        """
        if obj.type != "MESH":
            raise TypeError("Object must be a mesh")

        # Get the bounding box corners in local space
        local_bbox_corners = [mathutils.Vector(corner) for corner in obj.bound_box]

        # Convert to world coordinates
        world_bbox_corners = [obj.matrix_world @ corner for corner in local_bbox_corners]

        # Compute axis-aligned min/max coordinates
        min_corner = mathutils.Vector(map(min, zip(*world_bbox_corners, strict=False)))
        max_corner = mathutils.Vector(map(max, zip(*world_bbox_corners, strict=False)))

        return [[*min_corner], [*max_corner]]

    def get_object_info(self, name, sections=None, limit=100, offset=0):
        """
        Get detailed information about a specific object.

        `location`/`rotation`/`scale` are the object's local (parent-relative)
        transform; `world_bounding_box` (mesh objects only) is the world-space
        AABB computed via `matrix_world` (see `get_aabb`) - the two live in
        different spaces and are not directly comparable for a parented or
        transformed object.

        `rotation_mode` names how to read `rotation`: one of the six Euler
        orders ("XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX") means
        `[x, y, z]` radians in that order; "QUATERNION" means `[w, x, y, z]`;
        "AXIS_ANGLE" means `[angle, x, y, z]`. Reading `rotation` without
        checking `rotation_mode` will misinterpret non-Euler objects.

        `mesh.vertices`/`edges`/`polygons` are base-mesh (pre-modifier)
        counts, same caveat as `get_mesh_data`.

        Args:
            name: Name to assign or look up.

        Returns:
            Result produced by the operation.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        if sections is not None:
            allowed_sections = {
                "GEOMETRY",
                "ATTRIBUTES",
                "VOLUME_GRIDS",
                "GREASE_PENCIL",
                "PARTICLES",
                "SOFT_BODY",
                "DYNAMIC_PAINT",
            }
            sections = {str(section).upper() for section in sections}
            unknown = sorted(sections - allowed_sections)
            if unknown:
                raise ValueError(f"Unsupported object-info sections: {unknown}")
        else:
            sections = {
                "GEOMETRY",
                "ATTRIBUTES",
                "VOLUME_GRIDS",
                "GREASE_PENCIL",
                "PARTICLES",
                "SOFT_BODY",
                "DYNAMIC_PAINT",
            }
        start, _end, _truncated, _next = paginate(0, offset, limit, 1000)
        obj = find_object(bpy.data.objects, name)
        if not obj:
            raise ValueError(f"Object not found: {name}")
        sync_from_editmode(obj)

        if obj.rotation_mode == "QUATERNION":
            q = obj.rotation_quaternion
            rotation = [q.w, q.x, q.y, q.z]
        elif obj.rotation_mode == "AXIS_ANGLE":
            angle, x, y, z = obj.rotation_axis_angle
            rotation = [angle, x, y, z]
        else:
            rotation = [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z]

        # Basic object info
        obj_info = {
            "name": obj.name,
            "type": obj.type,
            "library": client_safe_name_leaf(obj.library.name) if getattr(obj, "library", None) else None,
            "is_override": getattr(obj, "override_library", None) is not None,
            "location": [obj.location.x, obj.location.y, obj.location.z],
            "rotation_mode": obj.rotation_mode,
            "rotation": rotation,
            "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            "matrix_world": [[float(value) for value in row] for row in obj.matrix_world],
            "dimensions": [float(value) for value in obj.dimensions],
            "parent": obj.parent.name if obj.parent else None,
            "parent_type": obj.parent_type,
            "parent_bone": obj.parent_bone or None,
            "collections": sorted(collection.name for collection in obj.users_collection),
            "data_name": obj.data.name if obj.data else None,
            "selected": bool(obj.select_get()),
            "visible": obj.visible_get(),
            "hide_viewport": bool(obj.hide_viewport),
            "hide_render": bool(obj.hide_render),
            "materials": [],
            "modifiers": [
                {
                    "name": m.name,
                    "type": m.type,
                    "show_viewport": m.show_viewport,
                    "show_render": m.show_render,
                }
                for m in obj.modifiers
            ],
        }

        if obj.type == "MESH":
            bounding_box = self.get_aabb(obj)
            obj_info["world_bounding_box"] = bounding_box

        # Add material slots
        for slot in obj.material_slots:
            if slot.material:
                obj_info["materials"].append(slot.material.name)

        # Add mesh data if applicable
        if obj.type == "MESH" and obj.data:
            mesh = obj.data
            obj_info["mesh"] = {
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
            }

        type_data = self._object_type_data(obj, sections, limit, start)
        if type_data:
            obj_info["type_data"] = type_data

        return obj_info

    @staticmethod
    def _page_records(records, offset, limit):
        total = len(records)
        start, end, truncated, next_offset = paginate(total, offset, limit, 1000)
        return {
            "total": total,
            "offset": start,
            "limit": limit,
            "returned_count": end - start,
            "truncated": truncated,
            "next_offset": next_offset,
            "records": records[start:end],
        }

    @staticmethod
    def _attribute_records(data):
        return [
            {
                "name": attribute.name,
                "data_type": attribute.data_type,
                "domain": attribute.domain,
                "count": len(attribute.data),
            }
            for attribute in getattr(data, "attributes", ())
        ]

    def _object_type_data(self, obj, sections, limit, offset):
        """Return bounded, explicitly local-space native data and simulation state."""
        result = {"coordinate_space": "OBJECT_LOCAL", "evaluated": False}
        data = obj.data
        if data is not None and "ATTRIBUTES" in sections and hasattr(data, "attributes"):
            result["attributes"] = self._page_records(self._attribute_records(data), offset, limit)

        if obj.type in {"CURVE", "SURFACE"} and "GEOMETRY" in sections:
            splines = []
            for index, spline in enumerate(data.splines):
                splines.append(
                    {
                        "index": index,
                        "type": spline.type,
                        "point_count_u": spline.point_count_u,
                        "point_count_v": spline.point_count_v,
                        "cyclic_u": spline.use_cyclic_u,
                        "cyclic_v": getattr(spline, "use_cyclic_v", False),
                        "order_u": getattr(spline, "order_u", None),
                        "order_v": getattr(spline, "order_v", None),
                        "resolution_u": spline.resolution_u,
                        "resolution_v": getattr(spline, "resolution_v", None),
                    }
                )
            result["curve"] = {
                "dimensions": data.dimensions,
                "resolution_u": data.resolution_u,
                "resolution_v": data.resolution_v,
                "bevel_depth": data.bevel_depth,
                "splines": self._page_records(splines, offset, limit),
            }
        elif obj.type == "CURVES" and "GEOMETRY" in sections:
            result["curves"] = {
                "point_count": len(data.points),
                "curve_count": len(data.curves),
                "surface": data.surface.name if getattr(data, "surface", None) else None,
            }
        elif obj.type == "POINTCLOUD" and "GEOMETRY" in sections:
            result["pointcloud"] = {"point_count": len(data.points)}
        elif obj.type == "VOLUME" and "VOLUME_GRIDS" in sections:
            grids = [
                {
                    "name": grid.name,
                    "data_type": grid.data_type,
                    "channels": grid.channels,
                    "voxel_size": list(grid.voxel_size),
                    "is_loaded": grid.is_loaded,
                }
                for grid in data.grids
            ]
            result["volume"] = {
                "filepath": data.filepath,
                "is_sequence": data.is_sequence,
                "frame_start": data.frame_start,
                "frame_duration": data.frame_duration,
                "grids": self._page_records(grids, offset, limit),
            }
        elif obj.type == "GREASEPENCIL" and "GREASE_PENCIL" in sections:
            layers = []
            for layer in data.layers:
                frames = list(layer.frames)
                layers.append(
                    {
                        "name": layer.name,
                        "frame_count": len(frames),
                        "frames": [
                            {
                                "frame_number": frame.frame_number,
                                "stroke_count": len(frame.drawing.strokes),
                                "point_count": len(frame.drawing.attributes["position"].data),
                            }
                            for frame in frames[:limit]
                        ],
                        "frames_truncated": len(frames) > limit,
                    }
                )
            result["grease_pencil"] = {"layers": self._page_records(layers, offset, limit)}

        if "PARTICLES" in sections:
            systems = [
                {
                    "name": system.name,
                    "settings": system.settings.name if system.settings else None,
                    "particle_count": len(system.particles),
                    "seed": system.seed,
                }
                for system in getattr(obj, "particle_systems", ())
            ]
            if systems:
                result["particle_systems"] = self._page_records(systems, offset, limit)
        if "SOFT_BODY" in sections and getattr(obj, "soft_body", None) is not None:
            soft_body = obj.soft_body
            point_cache = soft_body.point_cache
            result["soft_body"] = {
                "goal": soft_body.settings.use_goal,
                "self_collision": soft_body.settings.use_self_collision,
                "cache": {
                    "frame_start": point_cache.frame_start,
                    "frame_end": point_cache.frame_end,
                    "is_baked": point_cache.is_baked,
                    "is_baking": point_cache.is_baking,
                },
            }
        if "DYNAMIC_PAINT" in sections:
            states = []
            for modifier in obj.modifiers:
                if modifier.type != "DYNAMIC_PAINT":
                    continue
                states.append(
                    {
                        "modifier": modifier.name,
                        "ui_type": modifier.ui_type,
                        "canvas_active": modifier.canvas_settings is not None,
                        "brush_active": modifier.brush_settings is not None,
                    }
                )
            if states:
                result["dynamic_paint"] = self._page_records(states, offset, limit)
        return result if len(result) > 2 else {}

    _MESH_DATA_ELEMENT_TYPES = ("vertices", "edges", "faces", "loops")
    _MESH_DATA_MAX_LIMIT = 1000

    @staticmethod
    def _mesh_data_vertex(v):
        return {
            "index": v.index,
            "co": [v.co.x, v.co.y, v.co.z],
            "normal": [v.normal.x, v.normal.y, v.normal.z],
            "select": bool(v.select),
        }

    @staticmethod
    def _mesh_data_edge(e):
        return {
            "index": e.index,
            "vertices": list(e.vertices),
            "select": bool(e.select),
        }

    @staticmethod
    def _mesh_data_face(f):
        return {
            "index": f.index,
            "vertices": list(f.vertices),
            "normal": [f.normal.x, f.normal.y, f.normal.z],
            "select": bool(f.select),
            "material_index": f.material_index,
        }

    def get_mesh_data(self, object_name, element_type="vertices", limit=100, offset=0, selected_only=False):
        """
        Paginated inspection of a mesh's vertices/edges/faces/loops (indices, coords, normals, selection).

        Prerequisite for index-based edits: mesh_extrude/mesh_inset/mesh_bevel/
        mesh_bridge/mesh_subdivide take raw indices with no way to discover them
        otherwise, since get_object_info only reports element counts.

        Coordinates and normals come from the object's base mesh (`obj.data`)
        in local (object-space) coordinates - modifiers are not evaluated. To
        get world-space positions, transform by the object's `matrix_world`
        (see `get_object_info`).

        Args:
            object_name: Name of the Blender object to operate on.
            element_type: Value for element type.
            limit: Maximum number of items to return.
            offset: Zero-based starting position.
            selected_only: Value for selected only.

        Returns:
            Result produced by the operation.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        if element_type not in self._MESH_DATA_ELEMENT_TYPES:
            raise ValueError(f"Invalid element_type: {element_type}. Must be one of {self._MESH_DATA_ELEMENT_TYPES}")
        obj = get_mesh_object(object_name)
        sync_from_editmode(obj)
        mesh = obj.data

        if element_type == "vertices":
            all_elements = mesh.vertices
            to_dict = self._mesh_data_vertex
        elif element_type == "edges":
            all_elements = mesh.edges
            to_dict = self._mesh_data_edge
        elif element_type == "faces":
            all_elements = mesh.polygons
            to_dict = self._mesh_data_face
        else:
            if selected_only:
                raise ValueError(
                    "selected_only is not supported for element_type='loops': "
                    "MeshLoop has no selection state of its own (use 'vertices', "
                    "'edges', or 'faces' instead)"
                )
            all_elements = mesh.loops
            face_of_loop = {}
            for face in mesh.polygons:
                for loop_index in face.loop_indices:
                    face_of_loop[loop_index] = face.index
            if hasattr(mesh, "calc_normals_split"):
                mesh.calc_normals_split()

            # Named and then assigned, so `to_dict` stays an ordinary variable: a
            # `def to_dict` here would declare the loop signature for the whole
            # function and reject the three bound methods above it.
            def _loop_to_dict(loop):
                normal = loop.normal
                return {
                    "index": loop.index,
                    "vertex_index": loop.vertex_index,
                    "edge_index": loop.edge_index,
                    "face_index": face_of_loop.get(loop.index),
                    "normal": [normal.x, normal.y, normal.z],
                }

            to_dict = _loop_to_dict

        total_unfiltered = len(all_elements)
        if selected_only:
            universe = [el for el in all_elements if el.select]
            total = len(universe)
            start, end, truncated, next_offset = paginate(total, offset, limit, self._MESH_DATA_MAX_LIMIT)
            page = universe[start:end]
        else:
            total = total_unfiltered
            start, end, truncated, next_offset = paginate(total, offset, limit, self._MESH_DATA_MAX_LIMIT)
            # islice avoids materializing the whole (possibly huge) collection
            # when the caller only asked for a small page of it.
            page = itertools.islice(all_elements, start, end)
        elements = [to_dict(el) for el in page]

        return {
            "name": obj.name,
            "element_type": element_type,
            "total": total,
            "total_unfiltered": total_unfiltered,
            "offset": start,
            "limit": limit,
            "returned_count": len(elements),
            "truncated": truncated,
            "next_offset": next_offset,
            "elements": elements,
        }
