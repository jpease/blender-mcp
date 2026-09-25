"""
Rows guarding artefact truth.

What render settings report writing, what a save discards, whether a file resolves
elsewhere, its provenance, and render intent.

Label prefixes: `render settings:`, `transaction:`, `scene validation:`,
`scene transforms:`, `delivery:`, `provenance:`, `server tools:`, `rendering:`, `dispatch:`.
"""

from .common import (
    ADDON_DELIVERY,
    ADDON_FILE_LIFECYCLE,
    ADDON_LIBRARY_DIGEST,
    ADDON_PROVENANCE,
    ADDON_RENDERING,
    ADDON_SCENE,
    ADDON_SERVER_CORE,
    ADDON_SESSION,
    ADDON_TRANSACTION,
    DRT,
    FLT,
    MUTT,
    RENDT,
    SCENETOOLT,
    SERVER_FILE_LIFECYCLE_TOOL,
    SERVER_RENDERING_TOOL,
    SESSIONT,
    SFLT,
    SVT,
    Revert,
)

ROWS: list[Revert] = [
    # --- configure_render_settings answers with the paths it wrote, not the whole state ---
    Revert(
        "render settings: the reply carries the whole render state again instead of what it wrote",
        ADDON_RENDERING,
        "    after = {path: getattr(owner, name) for path, (owner, name) in applied.items()}\n",
        "    after = _render_info(scene)\n",
        (
            f"{RENDT}::test_configure_render_settings_returns_only_the_patched_values",
            f"{RENDT}::test_configure_render_settings_reports_a_patch_that_writes_nothing",
        ),
    ),
    Revert(
        "render settings: changed names the patch's top-level keys, not the property paths written",
        ADDON_RENDERING,
        '    changed = sorted([*applied, "frame_range_authored"] if authored_range else applied)\n',
        # `_patch_reply` is handed `applied`, not the patch, so the coarse top-level keys are
        # reconstructed from it: for a flat key the two are the same string, and for a nested
        # one `applied` carries "<section>.<key>" where the patch carried "<section>".
        '    changed = sorted({key.split(".")[0] for key in applied} | ({"frame_range_authored"} '
        "if authored_range else set()))\n",
        (f"{RENDT}::test_configure_render_settings_returns_only_the_patched_values",),
    ),
    Revert(
        "render settings: detail is ignored, so the before/after state is unreachable",
        ADDON_RENDERING,
        "    if detail:\n",
        "    if False:\n",
        (f"{RENDT}::test_configure_render_settings_detail_returns_both_full_state_blocks",),
    ),
    Revert(
        "render settings: configure_render_settings does not forward detail",
        SERVER_RENDERING_TOOL,
        '        {"scene_name": scene_name, "patch": patch.model_dump(exclude_none=True), "detail": detail},\n',
        '        {"scene_name": scene_name, "patch": patch.model_dump(exclude_none=True), "detail": False},\n',
        (f"{RENDT}::test_configure_render_settings_forwards_detail",),
    ),
    # --- artefact truth: the datablocks a save discards ---
    Revert(
        "transaction: a created datablock with no user is not reported as one the save discards",
        ADDON_TRANSACTION,
        '                if int(getattr(db, "users", 1) or 0) == 0',
        '                if int(getattr(db, "users", 1) or 0) < 0',
        (
            f"{MUTT}::test_persistence_an_unreferenced_created_datablock_is_reported",
            f"{MUTT}::test_persistence_the_warning_names_at_most_five_and_counts_the_rest",
        ),
    ),
    Revert(
        "transaction: a fake user is counted as no user, so a deliberate keep is reported as a loss",
        ADDON_TRANSACTION,
        '                if int(getattr(db, "users", 1) or 0) == 0',
        '                if int(getattr(db, "users", 1) or 0) == 0 or getattr(db, "use_fake_user", False)',
        (f"{MUTT}::test_persistence_a_fake_user_datablock_is_not_reported",),
    ),
    Revert(
        "transaction: a datablock with one real user is reported as unreferenced",
        ADDON_TRANSACTION,
        '                if int(getattr(db, "users", 1) or 0) == 0',
        '                if int(getattr(db, "users", 1) or 0) <= 1',
        (f"{MUTT}::test_persistence_an_assigned_datablock_is_not_reported",),
    ),
    Revert(
        "transaction: another file's datablock is claimed as this command's authorship",
        ADDON_TRANSACTION,
        "            if coll_name not in _AUTHORSHIP_EXEMPT_COLLECTIONS and "
        'getattr(datablock, "library", None) is None',
        "            if coll_name not in _AUTHORSHIP_EXEMPT_COLLECTIONS",
        (f"{MUTT}::test_persistence_a_linked_datablock_is_never_this_commands_authorship",),
    ),
    Revert(
        "transaction: the discard warning names every datablock instead of a bounded few",
        ADDON_TRANSACTION,
        "    shown = \", \".join(f\"{entry['collection']}:{entry['name']}\" "
        "for entry in entries[:MAX_REPORTED_UNREFERENCED])",
        "    shown = \", \".join(f\"{entry['collection']}:{entry['name']}\" for entry in entries)",
        (f"{MUTT}::test_persistence_the_warning_names_at_most_five_and_counts_the_rest",),
    ),
    Revert(
        "scene validation: the persistence domain stops reporting datablocks with no user",
        ADDON_SCENE,
        "            if users == 0:",
        "            if users < 0:",
        (
            f"{SVT}::test_persistence_findings_flag_unreferenced_and_fake_user_only_datablocks",
            f"{SVT}::test_persistence_findings_report_truncation_past_max_findings",
            f"{SVT}::test_validate_scene_runs_the_persistence_domain_on_request",
        ),
    ),
    Revert(
        "scene validation: an action kept alive only by a fake user is reported as driving something",
        ADDON_SCENE,
        '            elif coll_name == "actions" and getattr(datablock, "use_fake_user", False) and users <= 1:',
        "            elif False:",
        (f"{SVT}::test_persistence_findings_flag_unreferenced_and_fake_user_only_datablocks",),
    ),
    Revert(
        "scene validation: a linked datablock is reported as this file's to lose",
        ADDON_SCENE,
        '            if getattr(datablock, "library", None) is not None:\n                continue',
        "            if False:\n                continue",
        (f"{SVT}::test_persistence_findings_ignore_linked_datablocks",),
    ),
    Revert(
        "scene validation: the persistence domain is never run",
        ADDON_SCENE,
        '    if "persistence" in domains:',
        "    if False:",
        (f"{SVT}::test_validate_scene_runs_the_persistence_domain_on_request",),
    ),
    Revert(
        "scene transforms: a transform reply decomposes a matrix_world the graph has not evaluated",
        ADDON_SCENE,
        # The call alone appears at four sites; the `def` line above it names this one.
        "def _transform_snapshot(obj):\n    _update_view_layer()\n",
        "def _transform_snapshot(obj):\n",
        (f"{SCENETOOLT}::test_set_object_transform_reports_the_world_transform_the_scene_now_holds",),
    ),
    # --- artefact truth: will this file resolve elsewhere ---
    Revert(
        "delivery: a path outside the shot is published whole instead of by leaf",
        ADDON_DELIVERY,
        # `_published_page` is the one publisher every reference on a returned page goes
        # through, libraries included, so this anchor is every entry's path.
        "                **published_path_fields(\n"
        '                    raw, key="path", is_directory=is_directory, blank_is_unset=blank_is_unset, frame=frame\n'
        "                ),",
        '                "path": str(raw or ""),\n'
        '                "path_redacted": False,\n'
        '                "path_redaction_reason": None,',
        (
            f"{FLT}::test_inspect_delivery_reports_an_absolute_image_by_leaf_not_by_directory",
            f"{FLT}::test_inspect_delivery_does_not_mistake_a_rooted_triple_slash_path_for_a_relative_one",
        ),
    ),
    Revert(
        "delivery: relativity is judged by the // prefix, so a rooted path reads as portable",
        ADDON_DELIVERY,
        "    return relative_link_body(strip_unsafe(raw)) is not None",
        '    return str(raw or "").startswith("//")',
        (f"{FLT}::test_inspect_delivery_does_not_mistake_a_rooted_triple_slash_path_for_a_relative_one",),
    ),
    Revert(
        "delivery: packed pixels are judged by their path rather than by being packed",
        ADDON_DELIVERY,
        '        if getattr(image, "packed_file", None) is not None:\n            verdict = "PACKED"',
        '        if False:\n            verdict = "PACKED"',
        (f"{FLT}::test_inspect_delivery_reports_a_packed_image_as_portable",),
    ),
    Revert(
        "delivery: a broken image link is reported by path shape alone",
        ADDON_DELIVERY,
        '        elif image_path_missing(image):\n            verdict = "MISSING"',
        '        elif False:\n            verdict = "MISSING"',
        (f"{FLT}::test_inspect_delivery_reports_an_image_whose_file_is_gone_as_missing",),
    ),
    Revert(
        "delivery: portability is judged from the returned page, so a defect hides on page two",
        ADDON_DELIVERY,
        '    for entry in entries:\n        bucket = classes[entry["kind"]]',
        '    for entry in page:\n        bucket = classes[entry["kind"]]',
        (f"{FLT}::test_inspect_delivery_judges_portability_over_every_entry_not_the_returned_page",),
    ),
    Revert(
        "delivery: a cache that lives in memory is reported as a file that travels",
        ADDON_DELIVERY,
        '    if info.get("use_disk_cache"):\n        return "RELATIVE_OK" if bpy.data.filepath else "UNSET"\n'
        '    return "UNSET"',
        '    return "RELATIVE_OK"',
        (f"{FLT}::test_inspect_delivery_reports_a_memory_only_point_cache_as_unset",),
    ),
    Revert(
        "delivery: an unset Mantaflow cache directory passes as a portable default",
        ADDON_DELIVERY,
        '    verdict = "MISSING" if not resolved or not os.path.isdir(resolved) else _shape_verdict(raw)',
        "    verdict = _shape_verdict(raw)",
        (f"{FLT}::test_inspect_delivery_reports_an_unset_fluid_cache_directory_as_missing",),
    ),
    Revert(
        "delivery: hashing linked files needs no configured roots, making it a read oracle",
        ADDON_DELIVERY,
        '        roots = require_digest_roots("hash_libraries") if hash_libraries else []\n',
        "        roots = configured_file_roots()\n",
        (f"{FLT}::test_inspect_delivery_refuses_to_hash_libraries_without_configured_file_roots",),
        ("\n\nfrom ..output_roots import configured_file_roots\n"),
    ),
    Revert(
        "delivery: a library outside the roots is hashed anyway",
        ADDON_LIBRARY_DIGEST,
        "            enforce_roots(resolved, roots)",
        "            pass",
        (f"{FLT}::test_inspect_delivery_skips_hashing_a_library_outside_the_configured_roots",),
    ),
    Revert(
        "delivery: the page bounds are not checked before the scan walks bpy.data",
        ADDON_DELIVERY,
        "    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:",
        "    if False:",
        (f"{FLT}::test_inspect_delivery_refuses_an_out_of_range_page",),
    ),
    Revert(
        "delivery: an unsaved session is called portable, though // resolves against nothing",
        ADDON_DELIVERY,
        '        "portable": saved and not capped and unportable == 0,',
        '        "portable": not capped and unportable == 0,',
        (f"{FLT}::test_inspect_delivery_warns_that_an_unsaved_session_cannot_resolve_relative_paths",),
    ),
    # --- artefact truth: C2PA-shaped provenance in the file ---
    Revert(
        "provenance: the save writes no authorship block at all",
        ADDON_FILE_LIFECYCLE,
        (
            "        if request.write_provenance:\n"
            "            backup, ingredients = stamp_provenance(request.digest_roots, request.canonical)\n"
        ),
        (
            "        if False:\n"
            "            backup, ingredients = stamp_provenance(request.digest_roots, request.canonical)\n"
        ),
        (
            f"{FLT}::test_save_shot_writes_a_json_provenance_block_into_every_local_scene",
            f"{FLT}::test_save_shot_names_the_datablocks_this_session_authored",
        ),
    ),
    Revert(
        "provenance: write_provenance=false writes a block anyway",
        ADDON_FILE_LIFECYCLE,
        (
            "        if request.write_provenance:\n"
            "            backup, ingredients = stamp_provenance(request.digest_roots, request.canonical)\n"
        ),
        (
            "        if True:\n"
            "            backup, ingredients = stamp_provenance(request.digest_roots, request.canonical)\n"
        ),
        (f"{FLT}::test_save_shot_writes_nothing_when_provenance_is_declined",),
    ),
    Revert(
        "provenance: a linked scene is stamped with this file's authorship",
        ADDON_PROVENANCE,
        ("        if scene.library is not None:\n            continue"),
        ("        if False:\n            continue"),
        (f"{FLT}::test_save_shot_writes_a_json_provenance_block_into_every_local_scene",),
    ),
    Revert(
        "provenance: a save Blender refused leaves its claim on the scenes",
        ADDON_FILE_LIFECYCLE,
        ("    except RuntimeError as exc:\n        restore_provenance(backup)\n"),
        "    except RuntimeError as exc:\n",
        (f"{FLT}::test_a_failed_save_leaves_no_scene_claiming_provenance",),
    ),
    Revert(
        "provenance: checksums are taken with no roots to confine them",
        ADDON_LIBRARY_DIGEST,
        ("    roots = configured_file_roots()\n    if not roots:\n        raise ValueError(\n"),
        (
            "    roots = configured_file_roots()\n"
            "    if not roots:\n"
            '        return ["/"]\n'
            "    if not roots:\n"
            "        raise ValueError(\n"
        ),
        (f"{FLT}::test_save_shot_refuses_checksums_without_configured_file_roots",),
    ),
    Revert(
        "provenance: a library is hashed to the call's whole budget, not to the per-file bound",
        ADDON_PROVENANCE,
        "        library_digests(libraries, digest_roots, max_file_bytes=MAX_DIGEST_FILE_BYTES)\n",
        "        library_digests(libraries, digest_roots, max_file_bytes=MAX_DIGEST_TOTAL_BYTES)\n",
        (f"{FLT}::test_save_shot_bounds_each_library_hash_by_the_per_file_limit",),
        also="\nfrom ..file_digest import MAX_DIGEST_TOTAL_BYTES\n",
    ),
    Revert(
        "provenance: the block records no datablocks, so the file claims nothing was authored",
        ADDON_PROVENANCE,
        (
            "                \"datablocks\": [f\"{entry['collection']}:{entry['name']}\" "
            "for entry in authored.snapshot()],"
        ),
        '                "datablocks": [],',
        (f"{FLT}::test_save_shot_names_the_datablocks_this_session_authored",),
    ),
    Revert(
        "provenance: an oversized block is read back whole into the agent's context",
        ADDON_PROVENANCE,
        "    if not isinstance(raw, str) or len(raw) > MAX_PROVENANCE_CHARS:",
        "    if not isinstance(raw, str):",
        (f"{FLT}::test_inspect_delivery_reports_a_hostile_provenance_block_as_invalid[oversized]",),
    ),
    Revert(
        "provenance: unparseable text is reported as a valid block",
        ADDON_PROVENANCE,
        (
            "    except (TypeError, ValueError):\n"
            '        return {"present": True, "valid": False, "reason": "unparseable"}'
        ),
        (
            "    except (TypeError, ValueError):\n"
            '        return {"present": True, "valid": True, "reason": "unparseable"}'
        ),
        (f"{FLT}::test_inspect_delivery_reports_a_hostile_provenance_block_as_invalid[not json at all]",),
    ),
    Revert(
        "provenance: a JSON array is reported as a valid block",
        ADDON_PROVENANCE,
        (
            "    if not isinstance(block, dict):\n"
            '        return {"present": True, "valid": False, "reason": "unparseable"}'
        ),
        (
            "    if not isinstance(block, dict):\n"
            '        return {"present": True, "valid": True, "reason": "unparseable"}'
        ),
        (f"{FLT}::test_inspect_delivery_reports_a_hostile_provenance_block_as_invalid[[1, 2, 3]]",),
    ),
    Revert(
        "provenance: an ingredient list of any length is read back whole",
        ADDON_PROVENANCE,
        '        for entry in _bounded_dicts(block.get("ingredients"))\n',
        '        for entry in _unbounded_dicts(block.get("ingredients"))\n',
        (f"{FLT}::test_inspect_delivery_bounds_a_valid_provenance_block",),
        (
            "\n"
            "\n"
            "def _unbounded_dicts(value: object) -> list[dict]:\n"
            '    """\n'
            "    Reverted: `_bounded_dicts` without its `MAX_PROVENANCE_ENTRIES` cap.\n"
            "\n"
            "    Args:\n"
            "        value: The parsed value, of any shape.\n"
            "\n"
            "    Returns:\n"
            "        list[dict]: Every usable entry, however many the file carried.\n"
            "\n"
            '    """\n'
            "    if not isinstance(value, list):\n"
            "        return []\n"
            "    return [entry for entry in value if isinstance(entry, dict)]\n"
        ),
    ),
    Revert(
        "provenance: an ingredient is recorded from the file open before a save-as, naming the wrong place after it",
        ADDON_PROVENANCE,
        "    frame = path_frame(blend_filepath)\n",
        "    frame = path_frame()\n",
        (f"{FLT}::test_save_shot_records_each_ingredient_as_a_link_from_the_file_it_writes",),
    ),
    Revert(
        "provenance: any recorded redaction reason is relayed, so a stranger's text reaches the agent as a code",
        ADDON_PROVENANCE,
        '                if entry.get("filepath_redaction_reason") in PATH_REDACTION_REASONS\n',
        "                if True\n",
        (f"{FLT}::test_inspect_delivery_reads_back_only_a_known_ingredient_redaction_reason",),
    ),
    Revert(
        "provenance: a file with no block is reported as carrying an invalid one",
        ADDON_PROVENANCE,
        ("    if raw is None:\n        return None"),
        ('    if raw is None:\n        return {"present": True, "valid": False, "reason": "unparseable"}'),
        (f"{FLT}::test_inspect_delivery_reports_no_provenance_for_a_file_without_one",),
    ),
    Revert(
        "provenance: a completed load keeps the replaced session's authorship",
        ADDON_SESSION,
        "    authored.clear()\n    _STORE.state = applied_load_post(_STORE.state, file_path)",
        "    _STORE.state = applied_load_post(_STORE.state, file_path)",
        (f"{SESSIONT}::test_a_completed_load_forgets_what_the_replaced_session_authored",),
    ),
    Revert(
        "provenance: a load that never landed throws away the open file's authorship",
        ADDON_SESSION,
        "    _STORE.state = applied_load_failure(_STORE.state, file_path, is_directory=_names_a_directory(file_path))",
        "    authored.clear()\n"
        "    _STORE.state = applied_load_failure(_STORE.state, file_path, is_directory=_names_a_directory(file_path))",
        (f"{SESSIONT}::test_a_failed_load_keeps_the_open_files_authorship",),
    ),
    Revert(
        "provenance: an aborted swap keeps an authorship claim it can no longer describe",
        ADDON_SESSION,
        "    # The swap was aborted part-way: what is open cannot be described truthfully, so the\n"
        "    # authorship claim goes with it.\n"
        "    authored.clear()",
        "    # The swap was aborted part-way.",
        (f"{SESSIONT}::test_an_aborted_swap_forgets_the_authorship_it_can_no_longer_describe",),
    ),
    # --- artefact truth: the delivery tool's own surface ---
    Revert(
        "server tools: inspect_delivery drops the parameters it is given",
        SERVER_FILE_LIFECYCLE_TOOL,
        '            "hash_libraries": hash_libraries,\n            "max_hash_bytes": max_hash_bytes,',
        '            "hash_libraries": False,\n            "max_hash_bytes": 1,',
        (
            f"{SFLT}::test_inspect_delivery_forwards_every_parameter",
            f"{SFLT}::test_inspect_delivery_defaults_do_not_read_linked_files",
        ),
    ),
    Revert(
        "server tools: inspect_delivery's paging and hash bounds are undeclared",
        SERVER_FILE_LIFECYCLE_TOOL,
        "    limit: Annotated[int, Field(ge=1, le=200)] = 50,\n"
        "    offset: Annotated[int, Field(ge=0)] = 0,\n"
        "    hash_libraries: bool = False,\n"
        "    max_hash_bytes: Annotated[int, Field(ge=1, le=8 * 1024**3)] = 268_435_456,",
        "    limit: int = 50,\n"
        "    offset: int = 0,\n"
        "    hash_libraries: bool = False,\n"
        "    max_hash_bytes: int = 268_435_456,",
        tuple(
            f"{SFLT}::test_inspect_delivery_schema_rejects_out_of_range_paging[{case}]"
            for case in ("limit-0", "limit-201", "max_hash_bytes-0", "max_hash_bytes-8589934593", "offset--1")
        ),
    ),
    # --- artefact truth: render intent belongs to the scene ---
    Revert(
        "rendering: a render with no filepath falls back to Blender's own output path silently",
        ADDON_RENDERING,
        "    requested_filepath = filepath\n    if requested_filepath is None:",
        '    requested_filepath = filepath or "/tmp/fallback.png"\n    if False:',
        (
            f"{RENDT}::test_render_scene_without_a_filepath_names_the_tool_that_sets_one",
            f"{RENDT}::test_render_scene_renders_to_the_scenes_own_output_path",
        ),
    ),
    Revert(
        "rendering: an ANIMATION over Blender's untouched default range renders unasked",
        ADDON_RENDERING,
        "        and (frame_start, frame_end) == (1, 250)",
        "        and False",
        (f"{RENDT}::test_render_scene_refuses_an_animation_over_blenders_untouched_default_range",),
    ),
    Revert(
        "rendering: a frame range the MCP set still trips the default-range guard",
        ADDON_RENDERING,
        '        and not scene.get("blender_mcp_frame_range_authored", False)',
        "        and True",
        (f"{RENDT}::test_render_scene_accepts_the_default_range_when_it_was_chosen",),
    ),
    Revert(
        "rendering: configure_render_settings stops marking a frame range as authored",
        ADDON_RENDERING,
        '            scene["blender_mcp_frame_range_authored"] = True',
        "            pass",
        (f"{RENDT}::test_render_scene_accepts_the_default_range_when_it_was_chosen",),
    ),
    Revert(
        "rendering: inspect_render_output reads the caller's path text without resolving it",
        ADDON_RENDERING,
        "        resolved_output_path = _resolved_path(output_path) if output_path else None",
        "        resolved_output_path = output_path",
        (f"{RENDT}::test_inspect_render_output_reads_back_the_tilde_path_a_render_was_written_to",),
    ),
    Revert(
        "rendering: persist_output stores the resolved path instead of the caller's template",
        ADDON_RENDERING,
        "            scene.render.filepath = requested_filepath if persisted else original_path",
        "            scene.render.filepath = output if persisted else original_path",
        (f"{RENDT}::test_render_scene_persists_the_callers_template_not_the_resolved_path",),
    ),
    Revert(
        "rendering: every render stores its output path, whether or not it was asked to",
        ADDON_RENDERING,
        "            persisted = bool(persist_output) and not cancelled and completed",
        "            persisted = True",
        (
            f"{RENDT}::test_render_scene_leaves_the_output_path_alone_by_default",
            f"{RENDT}::test_render_scene_does_not_persist_a_cancelled_render",
        ),
    ),
    Revert(
        "rendering: a still's one-file path is stored as a per-frame template",
        ADDON_RENDERING,
        '        if persist_output and mode == "STILL":',
        "        if False:",
        (f"{RENDT}::test_render_scene_refuses_to_persist_a_still_path",),
    ),
    Revert(
        "rendering: a directory is accepted as the scene's stored output template",
        ADDON_RENDERING,
        '        _refuse_container_output(scene, pending["output"]["filepath"])',
        "        pass",
        (f"{RENDT}::test_configure_render_settings_refuses_a_directory_as_the_stored_template",),
    ),
    Revert(
        "rendering: the default reply carries every frame's bookkeeping again",
        ADDON_RENDERING,
        "    if not detail:",
        "    if False:",
        (f"{RENDT}::test_render_scene_reply_summarises_and_detail_restores_the_per_frame_arrays",),
    ),
    Revert(
        "dispatch: a render that stores its output template still bypasses the transaction",
        ADDON_SERVER_CORE,
        "            or (spec.non_undo_when is not None and spec.non_undo_when(params))",
        '            or cmd_type in {"render_scene"}',
        (f"{DRT}::test_a_render_that_persists_its_output_template_is_transacted",),
    ),
]
