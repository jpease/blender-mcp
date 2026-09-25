"""
Rows guarding camera placement and aim, and their neighbours.

Place and aim in one call, framing a rig on what it deforms, a retired socket,
`validate_scene`'s pages, and deleting an object as core work.

Label prefixes: `camera:`, `transport:`, `pose:`, `scene validation:`, `server tools:`.
"""

from .common import (
    ADDON_CAMERA_CORE,
    ADDON_CAMERA_SHARED,
    ADDON_CAMERA_TARGETING,
    ADDON_MANAGER,
    ADDON_POSING,
    ADDON_SCENE,
    AMT,
    BUNT,
    CAMT,
    CONNFAILT,
    CORET,
    POSET,
    SERVER_CAMERA_CORE_TOOL,
    SERVER_CAMERA_TARGETING_TOOL,
    SERVER_CONNECTION,
    SERVER_CORE_TOOL,
    SERVER_DOCUMENTATION,
    SERVER_SCENE_TOOL,
    SVT,
    Revert,
)

ROWS: list[Revert] = [
    # --- place and aim in one call, and refuse a vantage point on the target ---
    Revert(
        "camera: camera_location never leaves the server, so a placed aim moves nothing",
        SERVER_CAMERA_TARGETING_TOOL,
        '            "camera_location": camera_location,',
        '            "camera_location": None,',
        (f"{CAMT}::test_point_camera_at_places_before_aiming_and_refuses_a_coincident_placement",),
    ),
    Revert(
        "camera: a placement on the aim point is dispatched instead of refused before the round trip",
        SERVER_CAMERA_TARGETING_TOOL,
        " and tuple(camera_location) == tuple(target_point):",
        " and False:",
        (f"{CAMT}::test_point_camera_at_places_before_aiming_and_refuses_a_coincident_placement",),
    ),
    Revert(
        # Reverting `_set_world_location` itself proves nothing - no node reads the placed transform -
        # but reverting the guard reaches it, because the node's fake camera raises the moment its
        # matrix_world is touched. That is the claim: the refusal happens before the camera moves.
        "camera: the handler moves the camera onto the aim point before noticing the two coincide",
        ADDON_CAMERA_TARGETING,
        "            if (point - placement).length_squared <= _COINCIDENT_DISTANCE_SQUARED:",
        "            if False:",
        (f"{CAMT}::test_handler_point_camera_at_rejects_a_placement_on_the_aim_point",),
    ),
    Revert(
        "camera: a marker no longer says it claims the frames before it",
        ADDON_CAMERA_SHARED,
        '    if not camera_cuts or camera_cuts[0]["frame"] <= frame_start:',
        "    if True:",
        (f"{CAMT}::test_camera_markers_warn_that_the_earliest_marker_claims_every_frame_before_it",),
    ),
    Revert(
        # Both marker paths must warn in the same words, so the node compares set_scene_camera's
        # reply against create_camera_markers'. Silencing one side is the drift it exists to catch.
        "camera: set_scene_camera stops reporting the binding create_camera_markers reports",
        ADDON_CAMERA_CORE,
        '            "warnings": _retroactive_cut_warnings(scene.frame_start, _camera_cut_map(scene)),',
        '            "warnings": [],',
        (f"{CAMT}::test_setting_the_scene_camera_reports_the_retroactive_binding_in_the_same_words",),
    ),
    # --- what a socket the peer already retired, and a handshake that never ran, may claim ----
    Revert(
        # Emptying the set is the accurate revert: the retry stays, and nothing qualifies for it.
        # Widening it instead would be the dangerous direction, which the mutating-command node
        # beside this one is what catches.
        "transport: no command is worth resending, so a retired socket fails the call that found it",
        SERVER_CONNECTION,
        '_SIDE_EFFECT_FREE_COMMANDS = frozenset({"get_addon_info", "ping"})',
        "_SIDE_EFFECT_FREE_COMMANDS = frozenset()",
        (f"{CONNFAILT}::test_a_side_effect_free_command_is_resent_once_on_a_reconnected_socket",),
    ),
    Revert(
        # The zero-byte branch is the whole distinction: bytes arriving means the command was
        # serviced, so only a reply that never started is a candidate for a resend.
        "transport: a peer that closed before answering is indistinguishable from a cut-off reply",
        SERVER_CONNECTION,
        '                    raise BlenderPeerClosedError("Connection closed before receiving any data")',
        '                    raise Exception("Connection closed before receiving any data")',
        (f"{CONNFAILT}::test_a_side_effect_free_command_is_resent_once_on_a_reconnected_socket",),
    ),
    Revert(
        # Swallowing it again is what put `up_to_date=False`, `protocol_version=None` and an
        # empty capability list in front of an agent whose socket had simply gone away.
        "transport: a handshake that never completed is reported as an outdated add-on",
        ADDON_MANAGER,
        "        if _is_transport_failure(e):\n",
        "        if False:\n",
        (f"{AMT}::test_handshake_re_raises_a_transport_failure_instead_of_reporting_a_version",),
    ),
    # --- a rotation that cannot move the bone it names, and a rig framed on its silhouette ----
    Revert(
        # The notice is the only channel that can say it: the pose matrix changed, the
        # keys landed, and every other field in the reply reports a success. Only LOCAL and
        # LOCAL_WITH_PARENT resolve `Y` to the bone's own length, so skipping the space check
        # silences every roll notice this file makes.
        "pose: a rotation about the bone's own length axis is reported as if it moved something",
        ADDON_POSING,
        "    if space not in _BONE_LOCAL_SPACES:\n        return []\n",
        "    if True:\n        return []\n",
        (f"{POSET}::test_a_roll_that_moves_nothing_measurable_warns_and_quotes_what_it_measured",),
    ),
    Revert(
        # Dropping the expansion leaves the armature's own bounds, which are its bones and not
        # the silhouette the camera sees - the exact frame this parameter exists to replace.
        "camera: an armature frames on its bones instead of the meshes it deforms",
        ADDON_CAMERA_TARGETING,
        "    combined = list(objects)\n",
        "    combined = list(objects)\n    return combined\n",
        (f"{CAMT}::test_handler_framing_expands_an_armature_to_the_meshes_it_deforms",),
    ),
    Revert(
        # The status tool is the last place the distinction can be drawn: past this point the
        # agent has a payload and no way to tell "nothing was learned" from "the add-on is old".
        "transport: a status call renders a dead socket as a version verdict again",
        SERVER_CORE_TOOL,
        "    except (BlenderTransportError, ConnectionError) as exc:\n",
        "    except (BlenderTransportError, ConnectionError) as exc:\n        raise ToolError(str(exc)) from exc\n",
        (f"{CORET}::test_get_addon_status_reports_a_dead_socket_as_a_transport_failure",),
    ),
    Revert(
        # Widening the set is the dangerous direction: a mutating command resent blind doubles
        # an edit to the user's scene, and nothing downstream can tell that it did.
        "transport: a mutating command joins the resend set and is sent twice",
        SERVER_CONNECTION,
        '_SIDE_EFFECT_FREE_COMMANDS = frozenset({"get_addon_info", "ping"})',
        '_SIDE_EFFECT_FREE_COMMANDS = frozenset({"get_addon_info", "ping", "set_object_transform"})',
        (f"{CONNFAILT}::test_a_mutating_command_is_never_resent_after_the_peer_closed",),
    ),
    Revert(
        # A reply that started and stopped means Blender ran the command; resending it is the
        # same double-apply, arrived at from the other side.
        "transport: a reply cut off mid-message is treated as one that never started",
        SERVER_CONNECTION,
        '                raise Exception("Connection closed mid-message")',
        '                raise BlenderPeerClosedError("Connection closed mid-message")',
        (f"{CONNFAILT}::test_a_reply_cut_off_mid_message_is_not_resent_even_for_a_read_only_command",),
    ),
    Revert(
        # A rig that deforms nothing in this scene is the silent case: without the refusal the
        # expansion contributes no points and the camera frames whatever else was named.
        "camera: an armature that deforms nothing is expanded to an empty frame instead of refused",
        ADDON_CAMERA_TARGETING,
        "        if not meshes:\n",
        "        if False:\n",
        (f"{CAMT}::test_handler_framing_refuses_every_unresolvable_armature_name_before_touching_the_camera",),
    ),
    # --- validate_scene pages its findings, and bounds one finding's evidence ---
    Revert(
        "scene validation: the preflight forwards every page request as the first page",
        SERVER_SCENE_TOOL,
        '{"scene_name": scene_name, "scope": scope, "max_findings": max_findings, "offset": offset},',
        '{"scene_name": scene_name, "scope": scope, "max_findings": max_findings, "offset": 0},',
        (f"{SVT}::test_validate_scene_dispatches_scope_max_findings_and_offset",),
    ),
    Revert(
        # The tool's body forwards straight to the add-on, so the declared schema is the only
        # place a page request out of range is refused before a round trip is spent on it.
        "scene validation: offset is declared unbounded, so a negative page reaches the socket",
        SERVER_SCENE_TOOL,
        "    offset: Annotated[int, Field(ge=0, le=9999)] = 0,\n",
        "    offset: int = 0,\n",
        (f"{SVT}::test_validate_scene_offset_schema_rejects_out_of_range",),
    ),
    Revert(
        "scene validation: an offset out of range is discovered after every domain has been walked",
        ADDON_SCENE,
        '        if not 0 <= int(offset) <= 9999:\n            raise ValueError("offset must be in [0, 9999]")\n',
        '        if False:\n            raise ValueError("offset must be in [0, 9999]")\n',
        (f"{SVT}::test_validate_scene_rejects_out_of_range_offset_before_scanning_anything",),
    ),
    Revert(
        # The conflation `next_offset` pointed at nothing under: a domain that stopped at its own
        # internal cap is unreachable by any offset of this call, so reporting it on `truncated`
        # sent the caller after a page that does not exist.
        "scene validation: a domain that capped itself is reported as a cut page again",
        ADDON_SCENE,
        '            "truncated": truncated,\n',
        '            "truncated": truncated or domains_truncated,\n',
        (f"{SVT}::test_validate_scene_separates_a_cut_page_from_a_domain_that_capped_itself",),
    ),
    Revert(
        # The reply advertised `next_offset` while neither side accepted an offset, so every
        # resumed page handed back the findings the first page had already shown.
        "scene validation: the findings page always starts at the first finding",
        ADDON_SCENE,
        "        start, end, truncated, next_offset = paginate(len(findings), int(offset), int(max_findings), 1000)",
        "        start, end, truncated, next_offset = paginate(len(findings), 0, int(max_findings), 1000)",
        (f"{SVT}::test_validate_scene_offset_returns_the_next_findings_and_a_matching_next_offset",),
    ),
    Revert(
        # A sub-validator asked for `max_findings` alone cannot fill a page that starts past it:
        # it returns exactly what page one already showed, and the resumed page comes back empty.
        "scene validation: each domain is asked only for one page's worth of findings",
        ADDON_SCENE,
        "        domain_limit = min(int(offset) + int(max_findings), 1000)",
        "        domain_limit = int(max_findings)",
        (f"{SVT}::test_validate_scene_asks_each_domain_for_enough_findings_to_fill_a_resumed_page",),
    ),
    Revert(
        # `paginate` clamps the start to the total, so an over-run reads as the end of the list.
        # Echoing the request back instead reports a page starting where no finding is.
        "scene validation: an offset past the last finding is echoed back as where the page starts",
        ADDON_SCENE,
        '            "offset": start,\n',
        '            "offset": int(offset),\n',
        (f"{SVT}::test_validate_scene_offset_past_the_findings_returns_an_empty_final_page",),
    ),
    Revert(
        # One OVERLAPPING_UVS finding carries every overlapping face pair it found, and the
        # envelope cuts the longest list first - so that one finding used to push every other
        # domain's findings off the wire.
        "scene validation: a finding's list evidence is published whole, however long it is",
        ADDON_SCENE,
        "    if isinstance(evidence, list) and len(evidence) > _MAX_FINDING_EVIDENCE_ITEMS:\n",
        "    if False:\n",
        (f"{SVT}::test_validate_scene_bounds_one_findings_evidence_without_starving_the_others",),
    ),
    # --- deleting an object a session made is core work, not an authoring bundle's ---
    Revert(
        # The move from `scene_authoring.py` is across two files, which one anchor cannot
        # express; what a row can revert is the registration that puts the tool on the core
        # surface. Without it a session that made a scratch object has no way to take it back
        # out - `manage_scene_collections` refuses to unlink an object from its last collection -
        # so the object is saved into the shot for good, which is the state this replaced.
        "server tools: remove_scene_objects is not registered by the core scene module",
        SERVER_SCENE_TOOL,
        "@mcp.tool()\nasync def remove_scene_objects(",
        "async def remove_scene_objects(",
        (f"{BUNT}::test_removing_a_named_object_is_core_not_an_authoring_bundle",),
    ),
    Revert(
        # `reset_scene` is marked by name in `_DESTRUCTIVE_TOOLS`; this one earns the hint from
        # its prefix alone, because `confirm_remove` is not one of the conditional flags. Drop
        # the prefix and a core process deletes objects without warning first.
        "server tools: the remove_ prefix stops earning the destructive hint",
        SERVER_DOCUMENTATION,
        '    "remove_",\n',
        "",
        (f"{BUNT}::test_remove_scene_objects_advertises_its_destructiveness_from_the_core_surface",),
    ),
    # --- a new camera aims at a bone on the rig, and leaves no half-built camera behind ---
    Revert(
        # A bone names where on the target to look, so it travels with the object rather than
        # competing with it. Counted as a fifth source, naming both is refused and the aim the
        # parameter exists for cannot be asked for at all.
        "camera: a bone competes with the object it qualifies instead of travelling with it",
        SERVER_CAMERA_CORE_TOOL,
        "    orientations = [rotation_euler, rotation_quaternion, target_object_name, target_point]",
        "    orientations = [rotation_euler, rotation_quaternion, target_object_name, target_point, target_bone_name]",
        (f"{CAMT}::test_create_camera_treats_a_bone_as_a_qualifier_of_its_object_not_a_fifth_source",),
    ),
    Revert(
        # A character rig's origin is the floor under the character, so an object-only aim
        # frames the boots and misses the face the shot was set up for.
        "camera: a named bone is ignored, so the camera aims at the rig's origin",
        ADDON_CAMERA_SHARED,
        "    if not target_bone_name:\n",
        "    if True:\n",
        (f"{CAMT}::test_handler_create_camera_aims_at_the_named_bone_not_the_rig_origin",),
    ),
    Revert(
        # Falling back to the origin renders the misaimed shot the typo asked for, and reports
        # it as a success. The raise is left in place but unreachable, so the reverted form is
        # the silent fallback rather than a crash on the missing bone.
        "camera: a bone the armature does not have falls back to the origin instead of refusing",
        ADDON_CAMERA_SHARED,
        '    if target.type != "ARMATURE" or bone is None:\n',
        (
            '    if target.type != "ARMATURE" or bone is None:\n'
            "        return target.matrix_world.translation.copy()\n"
            "    if False:\n"
        ),
        (f"{CAMT}::test_handler_create_camera_refuses_a_bone_the_armature_does_not_have",),
    ),
    Revert(
        # Optics and the aim are only checkable against the camera they are being written to, so
        # both can still refuse after the datablocks exist. A direct caller - the real-Blender
        # smoke scripts - has no transaction to unwind with, and the refusal strands a
        # half-built camera plus an orphan `<name> Data` under a name the next attempt cannot
        # reuse cleanly.
        "camera: a refused configuration strands the camera object and its data in the file",
        ADDON_CAMERA_CORE,
        (
            "            bpy.data.objects.remove(obj, do_unlink=True)\n"
            "            bpy.data.cameras.remove(data, do_unlink=True)\n"
            "            raise\n"
        ),
        "            raise\n",
        (f"{CAMT}::test_handler_create_camera_removes_both_datablocks_when_configuration_is_refused",),
    ),
]
