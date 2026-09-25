"""
Rows guarding the linking handlers.

Link, override, list, reload, relocate and unlink, and the refusal to run a `.blend`'s
scripts.

Label prefixes: `linking:`, `polyhaven:`, `file paths:`.
"""

from .common import (
    ADDON_BLEND_FILES,
    ADDON_CANDIDATES,
    ADDON_FILE_PATHS,
    ADDON_LINKING,
    ADDON_POLYHAVEN,
    ADDON_SERVER_CORE,
    FPT,
    LKT,
    PHT,
    Revert,
)

# Appended by the four `stats Library.name` rows. `client_safe_leaf` no longer probes
# anything - the caller that holds the path passes `is_directory` in - so a revert that
# merely swapped the two leaf rules would be a no-op and its node would keep passing.
# This writes the probe back out. It imports `os` itself and leans on the
# `client_safe_name_leaf` every one of those call sites already has in scope: a row has
# one anchor and cannot add an import.
REVERTED_STATTING_NAME = '''

def _reverted_statting_name(name: object) -> str:
    """
    Reverted: the leaf rule as it was when it stat-ed the name itself.

    Args:
        name: The raw `Library.name`.

    Returns:
        str: The leaf, or `the requested file` when the filesystem calls the name a directory.

    """
    import os

    if os.path.isdir(str(name or "")):
        return "the requested file"
    return client_safe_name_leaf(name)
'''

ROWS: list[Revert] = [
    # --- link_canon_library, create_override, list/reload/relocate/unlink ---
    Revert(
        "linking: link hands the raw path to Blender without roots or file checks",
        ADDON_LINKING,
        (
            "        canonical = checked_blend_path(filepath, must_exist=True)\n"
            '        refuse_scripts_auto_execute("link_canon_library")\n'
        ),
        ('        canonical = str(filepath)\n        refuse_scripts_auto_execute("link_canon_library")\n'),
        (f"{LKT}::test_link_validates_its_path_before_blender_reads_it", f"{LKT}::test_link_enforces_the_file_roots"),
    ),
    Revert(
        "linking: absent names are not refused inside the load block",
        ADDON_LINKING,
        "            _refuse_absent_names(data_from, collection_names, object_names, world_names)\n",
        "",
        (f"{LKT}::test_link_refuses_a_name_absent_from_the_file_and_leaves_no_library",),
    ),
    Revert(
        "linking: link_canon_library joins the replacing set, so a failed link keeps its Library",
        ADDON_SERVER_CORE,
        '        "link_canon_library": CommandSpec(),',
        '        "link_canon_library": CommandSpec(datablock_replacing=True),',
        (
            f"{LKT}::test_a_link_that_fails_after_linking_rolls_its_library_back",
            f"{LKT}::test_the_three_replacing_commands_never_enter_a_transaction_and_the_link_does",
        ),
    ),
    Revert(
        "linking: reload_library leaves the replacing set and is transacted",
        ADDON_SERVER_CORE,
        '        "reload_library": CommandSpec(datablock_replacing=True),',
        '        "reload_library": CommandSpec(),',
        (f"{LKT}::test_the_three_replacing_commands_never_enter_a_transaction_and_the_link_does",),
    ),
    Revert(
        "linking: a linked collection is not instanced, so the next save drops it",
        ADDON_LINKING,
        "    _link_into(scene.collection.children, linked_collections)  # type: ignore[attr-defined]\n",
        "",
        (f"{LKT}::test_link_instances_what_it_linked_so_a_save_keeps_it",),
    ),
    Revert(
        "linking: collections defaults to a mutable list",
        ADDON_LINKING,
        "        collections: object = None,\n",
        "        collections: object = [],  # noqa: B006\n",
        (f"{LKT}::test_link_names_default_to_none_and_are_not_shared_across_calls",),
    ),
    Revert(
        "linking: the link passes create_liboverrides (Route A) when as_override is set",
        ADDON_LINKING,
        "bpy.data.libraries.load(canonical, link=True, relative=relative)",
        "bpy.data.libraries.load(canonical, link=True, relative=relative, create_liboverrides=as_override)",
        (f"{LKT}::test_link_as_override_uses_route_c_not_create_liboverrides",),
    ),
    Revert(
        "linking: as_override with objects is not refused",
        ADDON_LINKING,
        "        if as_override and object_names:\n",
        "        if False:\n",
        (f"{LKT}::test_link_refuses_as_override_with_objects",),
    ),
    Revert(
        "linking: relative is accepted in a never-saved session",
        ADDON_LINKING,
        "        if relative and not bpy.data.filepath:\n",
        "        if False:\n",
        (f"{LKT}::test_link_refuses_relative_in_a_never_saved_session",),
    ),
    Revert(
        "linking: link flags are coerced with bool()",
        ADDON_LINKING,
        (
            '        as_override = require_bool("as_override", as_override)\n'
            '        relative = require_bool("relative", relative)\n'
        ),
        ("        as_override = bool(as_override)\n        relative = bool(relative)\n"),
        (
            f"{LKT}::test_link_flags_must_be_real_bools[as_override]",
            f"{LKT}::test_link_flags_must_be_real_bools[relative]",
        ),
    ),
    Revert(
        "linking: a libraries.load failure goes out raw",
        ADDON_LINKING,
        'raise RuntimeError(operator_failure_message("link_canon_library", exc, (filepath, canonical))) from exc',
        "raise RuntimeError(str(exc)) from exc",
        (f"{LKT}::test_a_blender_link_failure_reaches_the_client_without_its_path",),
    ),
    Revert(
        "linking: do_fully_editable is inherited (Route B, system overrides)",
        ADDON_LINKING,
        "            do_fully_editable=True,\n",
        "",
        (
            f"{LKT}::test_create_override_passes_do_fully_editable_true_explicitly",
            f"{LKT}::test_link_as_override_uses_route_c_not_create_liboverrides",
            f"{LKT}::test_create_override_reports_same_named_linked_and_override_objects_distinguishably",
        ),
    ),
    Revert(
        "linking: create_override takes the scene from bpy.context",
        ADDON_LINKING,
        '        report = _override_all([collection], _scene(scene_uid), detail=require_bool("detail", detail))[0]\n',
        '        report = _override_all([collection], bpy.context.scene, detail=require_bool("detail", detail))[0]\n',
        (f"{LKT}::test_create_override_takes_the_scene_from_bpy_data_not_bpy_context",),
    ),
    Revert(
        "linking: create_override takes the view layer from bpy.context",
        ADDON_LINKING,
        "            scene.view_layers[0],  # type: ignore[attr-defined]\n",
        "            bpy.context.view_layer,\n",
        (f"{LKT}::test_create_override_takes_the_scene_from_bpy_data_not_bpy_context",),
    ),
    Revert(
        "linking: with several scenes the first is used silently",
        ADDON_LINKING,
        "    if len(scenes) != 1:\n",
        "    if False:\n",
        (f"{LKT}::test_create_override_refuses_to_guess_between_scenes",),
    ),
    Revert(
        "linking: a local or override collection is not refused before Blender is asked",
        ADDON_LINKING,
        '    if getattr(collection, "library", None) is None:\n',
        "    if False:\n",
        (f"{LKT}::test_create_override_resolves_by_session_uid_among_same_named_collections",),
    ),
    Revert(
        "linking: an already-overridden collection is overridden again",
        ADDON_LINKING,
        "    if existing:\n",
        "    if False:\n",
        (f"{LKT}::test_create_override_resolves_by_session_uid_among_same_named_collections",),
    ),
    Revert(
        "linking: a bool or float resolves the datablock whose uid equals it",
        ADDON_LINKING,
        "    if isinstance(value, bool) or not isinstance(value, int):\n",
        "    if not isinstance(value, (int, float)):\n",
        (
            f"{LKT}::test_create_override_refuses_a_uid_that_is_not_an_integer[True]",
            f"{LKT}::test_create_override_refuses_a_uid_that_is_not_an_integer[1.0]",
            f"{LKT}::test_unlink_requires_an_explicit_bounded_uid_list[bool]",
        ),
    ),
    Revert(
        "linking: an override's reference is not reported",
        ADDON_LINKING,
        '        "reference_uid": _uid_of(getattr(override, "reference", None)),\n',
        '        "reference_uid": None,\n',
        (f"{LKT}::test_create_override_reports_same_named_linked_and_override_objects_distinguishably",),
    ),
    Revert(
        "linking: the linked instance stays beside its override",
        ADDON_LINKING,
        "            parent.children.unlink(collection)  # type: ignore[attr-defined]\n",
        "            pass\n",
        (f"{LKT}::test_create_override_replaces_the_linked_instance_it_overrides",),
    ),
    Revert(
        "linking: a None override is not checked",
        ADDON_LINKING,
        "    if override is None:\n",
        "    if False:\n",
        (f"{LKT}::test_create_override_that_blender_declines_is_an_error_and_rolls_back",),
    ),
    Revert(
        "linking: an override failure goes out raw",
        ADDON_LINKING,
        'raise RuntimeError(operator_failure_message("create_override", exc, known)) from exc',
        "raise RuntimeError(str(exc)) from exc",
        (f"{LKT}::test_an_override_failure_reaches_the_client_sanitized",),
    ),
    Revert(
        "linking: list_libraries is not read-only, so it pays for a transaction",
        ADDON_SERVER_CORE,
        '        "list_libraries": CommandSpec(read_only=True),',
        '        "list_libraries": CommandSpec(),',
        (
            f"{LKT}::test_list_libraries_is_a_read_only_command_and_never_enters_a_transaction",
            f"{LKT}::test_the_linking_commands_are_dispatchable_and_advertised",
            f"{LKT}::test_the_three_replacing_commands_never_enter_a_transaction_and_the_link_does",
        ),
    ),
    Revert(
        "linking: list_libraries ignores offset",
        ADDON_LINKING,
        "        page = libraries[offset : offset + limit]\n",
        "        page = libraries[:limit]\n",
        (f"{LKT}::test_list_libraries_paginates_and_reports_what_a_reload_decision_needs",),
    ),
    Revert(
        "linking: list_libraries page bounds are coerced, not checked",
        ADDON_LINKING,
        '        limit = _bounded_int("limit", limit, 1, MAX_PAGE_SIZE)\n'
        '        offset = _bounded_int("offset", offset, 0, None)\n',
        "        limit, offset = int(limit), int(offset)  # type: ignore[arg-type]\n",
        tuple(
            f"{LKT}::test_list_libraries_bounds_its_page[{case}]"
            for case in ("zero", "over-bound", "bool-limit", "string-limit", "negative-offset", "bool-offset")
        ),
    ),
    Revert(
        "linking: needs_liboverride_resync is not reported",
        ADDON_LINKING,
        '        "needs_liboverride_resync": bool(getattr(library, "needs_liboverride_resync", False)),\n',
        '        "needs_liboverride_resync": False,\n',
        (f"{LKT}::test_list_libraries_paginates_and_reports_what_a_reload_decision_needs",),
    ),
    Revert(
        "linking: linked datablocks are listed without their uids",
        ADDON_LINKING,
        '        "session_uid": _uid_of(datablock),\n        "name": _display_name(datablock),\n        "id_type"',
        '        "session_uid": None,\n        "name": _display_name(datablock),\n        "id_type"',
        (
            f"{LKT}::test_list_libraries_paginates_and_reports_what_a_reload_decision_needs",
            f"{LKT}::test_reload_library_uses_the_data_api_inside_the_replace_flag",
        ),
    ),
    Revert(
        "linking: the per-library datablock list is uncapped",
        ADDON_LINKING,
        "    shown = items[:limit]\n",
        "    shown = list(items)\n",
        (f"{LKT}::test_list_libraries_bounds_the_datablocks_it_lists_per_library",),
    ),
    Revert(
        "linking: the reload is not wrapped in replacing_library_contents",
        ADDON_LINKING,
        "        with replacing_library_contents():\n",
        "        if True:\n",
        (f"{LKT}::test_reload_library_uses_the_data_api_inside_the_replace_flag",),
    ),
    Revert(
        "linking: the reload goes through wm.lib_reload",
        ADDON_LINKING,
        "            library.reload()  # type: ignore[attr-defined]\n",
        "            bpy.ops.wm.lib_reload(library=library.name)  # type: ignore[attr-defined]\n",
        (
            f"{LKT}::test_no_wm_lib_operator_exists_in_the_linking_module",
            f"{LKT}::test_reload_library_uses_the_data_api_inside_the_replace_flag",
        ),
    ),
    Revert(
        "linking: a reload failure goes out raw",
        ADDON_LINKING,
        "        raise RuntimeError(operator_failure_message(command, exc, known_paths)) from exc\n",
        '        raise RuntimeError(f"{command} failed: {exc}") from exc\n',
        (
            f"{LKT}::test_reload_failure_reaches_the_client_sanitized_from_a_captured_blender_error",
            f"{LKT}::test_reload_failure_of_a_relative_link_under_a_comma_directory_is_sanitized",
            f"{LKT}::test_a_failed_relocate_restores_the_previous_path_and_is_sanitized",
        ),
    ),
    Revert(
        "linking: relative known paths reach the sanitizer and eat the library name",
        ADDON_LINKING,
        "    return tuple(path for path in paths if isinstance(path, str) and os.path.isabs(path))\n",
        "    return tuple(path for path in paths if isinstance(path, str))\n",
        (f"{LKT}::test_a_reload_failure_in_an_unsaved_session_still_names_the_library",),
    ),
    Revert(
        "linking: relocate hands the raw path to Blender without roots or file checks",
        ADDON_LINKING,
        (
            "        canonical = checked_blend_path(filepath, must_exist=True)\n"
            "        for other in bpy.data.libraries:\n"
        ),
        ("        canonical = str(filepath)\n        for other in bpy.data.libraries:\n"),
        (
            f"{LKT}::test_relocate_validates_the_new_path_through_the_roots",
            f"{LKT}::test_relocate_assigns_the_canonical_path_reloads_and_reports_the_name_both_sides",
        ),
    ),
    Revert(
        "linking: relocate to a file another library already links",
        ADDON_LINKING,
        "            if other.session_uid != library.session_uid and canonical in _library_paths(other):\n",
        "            if False:\n",
        (f"{LKT}::test_relocate_refuses_a_file_another_library_already_links",),
    ),
    Revert(
        "linking: a failed relocate leaves the library pointing at the new file",
        ADDON_LINKING,
        "            library.filepath = previous  # type: ignore[attr-defined]\n            raise\n",
        "            raise\n",
        (f"{LKT}::test_a_failed_relocate_restores_the_previous_path_and_is_sanitized",),
    ),
    Revert(
        "linking: relocate stores the path as the client spelled it",
        ADDON_LINKING,
        "        library.filepath = canonical  # type: ignore[attr-defined]\n",
        "        library.filepath = filepath  # type: ignore[attr-defined]\n",
        (f"{LKT}::test_relocate_assigns_the_canonical_path_reloads_and_reports_the_name_both_sides",),
    ),
    Revert(
        "linking: an unknown uid refusal does not say where to read a current one",
        ADDON_LINKING,
        '        "library reload, so read a current one from list_libraries"\n',
        '        "library reload"\n',
        (
            f"{LKT}::test_an_unknown_library_uid_is_refused[reload_library]",
            f"{LKT}::test_an_unknown_library_uid_is_refused[relocate_library]",
        ),
    ),
    Revert(
        "linking: unlink runs without confirm",
        ADDON_LINKING,
        "    if not confirm:\n",
        "    if False:\n",
        (f"{LKT}::test_unlink_refuses_without_a_real_confirmation[false]",),
    ),
    Revert(
        "linking: unlink's confirm is coerced with bool()",
        ADDON_LINKING,
        '        confirm = require_bool("confirm", confirm)\n',
        "        confirm = bool(confirm)\n",
        (
            f"{LKT}::test_unlink_refuses_without_a_real_confirmation[string]",
            f"{LKT}::test_unlink_refuses_without_a_real_confirmation[int]",
        ),
    ),
    Revert(
        "linking: unlink removes every library, not only the named ones",
        ADDON_LINKING,
        "            bpy.data.libraries.remove(current)\n",
        "            for everything in list(bpy.data.libraries):\n"
        "                bpy.data.libraries.remove(everything)\n",
        (f"{LKT}::test_unlink_never_touches_a_library_that_was_not_named",),
    ),
    Revert(
        "linking: unknown uids are skipped instead of refusing the request",
        ADDON_LINKING,
        "    libraries = [_library(uid) for uid in uids]\n",
        "    libraries = [lib for lib in bpy.data.libraries if lib.session_uid in uids]\n",
        (f"{LKT}::test_unlink_resolves_every_uid_before_removing_anything",),
    ),
    Revert(
        "linking: the uid list is neither non-empty nor bounded",
        ADDON_LINKING,
        "    if not isinstance(library_uids, list) or not 0 < len(library_uids) <= MAX_UNLINK_UIDS:\n",
        "    if not isinstance(library_uids, list):\n",
        (
            f"{LKT}::test_unlink_requires_an_explicit_bounded_uid_list[empty]",
            f"{LKT}::test_unlink_requires_an_explicit_bounded_uid_list[big]",
        ),
    ),
    Revert(
        "linking: uid list entries are coerced with int()",
        ADDON_LINKING,
        '    uids = list(dict.fromkeys(_require_uid("library_uids entry", uid) for uid in library_uids))\n',
        "    uids = list(dict.fromkeys(int(uid) for uid in library_uids))\n",
        (
            f"{LKT}::test_unlink_requires_an_explicit_bounded_uid_list[bool]",
            f"{LKT}::test_unlink_requires_an_explicit_bounded_uid_list[string]",
        ),
    ),
    Revert(
        "linking: the removal report counts only the libraries",
        ADDON_LINKING,
        '            "removed_by_type": summarize_type_counts(before[uid].collection for uid in removed),\n',
        '            "removed_by_type": {"libraries": len(removed_libraries)},\n',
        (f"{LKT}::test_unlink_reports_exactly_what_it_removed",),
    ),
    Revert(
        "linking: the unlink's sample of what it removed is unbounded, so a furnished set sends every name",
        ADDON_LINKING,
        "                for uid in removed[:MAX_LISTED_NAMES]\n",
        "                for uid in removed\n",
        (f"{LKT}::test_unlinking_a_large_library_counts_what_went_and_names_only_a_sample",),
    ),
    # One node, not two: `_newly_orphaned` keys its candidates by uid now, so a doubled walk
    # can no longer hand `batch_remove` duplicates and the purge test stopped noticing this
    # revert. The removal report still does - every uid's collection name becomes `all_ids`.
    Revert(
        "linking: the census walks bpy.data.all_ids, counting everything twice",
        ADDON_LINKING,
        '        aggregate = getattr(getattr(prop, "fixed_type", None), "identifier", None) == "ID"\n',
        "        aggregate = False\n",
        (f"{LKT}::test_unlink_reports_exactly_what_it_removed",),
    ),
    Revert(
        "linking: an indirect library is unlinked",
        ADDON_LINKING,
        "    if indirect:\n",
        "    if False:\n",
        (f"{LKT}::test_unlink_refuses_an_indirect_library",),
    ),
    Revert(
        "linking: orphans are purged without purge_orphans",
        ADDON_LINKING,
        "        purged = _purge_newly_orphaned(before, known) if purge_orphans else []\n",
        "        purged = _purge_newly_orphaned(before, known)\n",
        (f"{LKT}::test_unlink_purges_only_when_asked_and_only_what_it_orphaned",),
    ),
    Revert(
        "linking: the purge also takes datablocks that were orphans before the unlink",
        ADDON_LINKING,
        "if users == 0 and uid in before and before[uid].users > 0]",
        "if users == 0]",
        (
            f"{LKT}::test_unlink_purges_only_when_asked_and_only_what_it_orphaned",
            # The same clause at `compute_orphaned`, the pure rule the purge is built on:
            # without it every zero-user datablock is this unlink's orphan, which is the
            # mistake `orphans_purge` makes.
            f"{LKT}::test_only_a_datablock_this_unlink_emptied_counts_as_orphaned",
        ),
    ),
    Revert(
        "linking: a library is removed through a reference an earlier removal may have freed",
        ADDON_LINKING,
        "        current = next((library for library in bpy.data.libraries if library.session_uid == uid), None)\n",
        "        current = next((library for library in libraries if library.session_uid == uid), None)\n",
        (f"{LKT}::test_unlink_never_removes_a_datablock_an_earlier_removal_freed",),
    ),
    Revert(
        "linking: a libraries.remove failure goes out raw",
        ADDON_LINKING,
        '            message = operator_failure_message("unlink_libraries", exc, known_paths)\n',
        "            message = str(exc)\n",
        (f"{LKT}::test_an_unlink_failure_reaches_the_client_sanitized",),
    ),
    Revert(
        "linking: a part-way unlink failure reports what it removed as prose, not as data",
        ADDON_LINKING,
        "            raise PartialUnlinkError(message, removed, already_removed) from exc\n",
        '            raise RuntimeError(f"{message} (libraries already removed: {removed})") from exc\n',
        (f"{LKT}::test_a_part_way_unlink_failure_carries_what_it_already_removed",),
    ),
    # `linking: the name helper returns the first of several matches` stood here. The helper
    # it guarded, `resolve_unique_name`, is gone: no command took a datablock name as a
    # handle, so nothing called it and its test went with it.
    Revert(
        "linking: create_override grows a name handle",
        ADDON_LINKING,
        "    def create_override(\n"
        "        collection_uid: object, *, scene_uid: object = None, detail: object = False\n"
        "    ) -> dict[str, object]:\n",
        "    def create_override(\n"
        "        collection_uid: object, *, scene_uid: object = None, detail: object = False,\n"
        "        collection_name: object = None,\n"
        "    ) -> dict[str, object]:\n",
        (f"{LKT}::test_no_linking_command_takes_a_datablock_name_as_a_handle",),
    ),
    Revert(
        "linking: unlink_libraries is not registered",
        ADDON_SERVER_CORE,
        '        "unlink_libraries": CommandSpec(datablock_replacing=True),\n',
        "",
        (
            f"{LKT}::test_the_linking_commands_are_dispatchable_and_advertised",
            f"{LKT}::test_the_three_replacing_commands_never_enter_a_transaction_and_the_link_does",
        ),
    ),
    # --- script auto-execution refusals (a .blend's scripts must never run) ---
    Revert(
        "linking: link_canon_library does not check use_scripts_auto_execute",
        ADDON_LINKING,
        '        refuse_scripts_auto_execute("link_canon_library")\n',
        "",
        (
            f"{LKT}::test_link_refuses_while_scripts_auto_execute_is_on[on]",
            f"{LKT}::test_link_refuses_while_scripts_auto_execute_is_on[unreadable]",
        ),
    ),
    Revert(
        "linking: reload_library does not check use_scripts_auto_execute",
        ADDON_LINKING,
        '        refuse_scripts_auto_execute("reload_library")\n',
        "",
        (
            f"{LKT}::test_reload_and_relocate_refuse_while_scripts_auto_execute_is_on[on]",
            f"{LKT}::test_reload_and_relocate_refuse_while_scripts_auto_execute_is_on[unreadable]",
        ),
    ),
    Revert(
        "linking: relocate_library does not check use_scripts_auto_execute",
        ADDON_LINKING,
        '        refuse_scripts_auto_execute("relocate_library")\n',
        "",
        (
            f"{LKT}::test_reload_and_relocate_refuse_while_scripts_auto_execute_is_on[on]",
            f"{LKT}::test_reload_and_relocate_refuse_while_scripts_auto_execute_is_on[unreadable]",
        ),
    ),
    Revert(
        "linking: the scripts check refuses even with the preference off",
        ADDON_BLEND_FILES,
        '    if getattr(filepaths, "use_scripts_auto_execute", True) is not False:\n',
        "    if True:\n",
        (
            f"{LKT}::test_link_proceeds_while_scripts_auto_execute_is_off",
            f"{LKT}::test_reload_and_relocate_proceed_while_scripts_auto_execute_is_off",
            f"{LKT}::test_create_override_proceeds_while_scripts_auto_execute_is_off",
            f"{PHT}::test_a_downloaded_blend_is_loaded_while_scripts_auto_execute_is_off",
        ),
    ),
    Revert(
        "linking: the scripts refusal always names open_shot",
        ADDON_BLEND_FILES,
        '            f"{command} refuses to load while',
        '            f"open_shot refuses to load while',
        (
            f"{LKT}::test_link_refuses_while_scripts_auto_execute_is_on[on]",
            f"{PHT}::test_a_downloaded_blend_is_never_loaded_while_scripts_auto_execute_is_on[on]",
        ),
    ),
    Revert(
        "polyhaven: the Poly Haven .blend import does not check use_scripts_auto_execute",
        ADDON_POLYHAVEN,
        '    refuse_scripts_auto_execute("import_polyhaven_asset")\n',
        "",
        (
            f"{PHT}::test_a_downloaded_blend_is_never_loaded_while_scripts_auto_execute_is_on[on]",
            f"{PHT}::test_a_downloaded_blend_is_never_loaded_while_scripts_auto_execute_is_on[unreadable]",
        ),
    ),
    # --- override re-linking, reload placeholders, relocation, quoted library names ---
    Revert(
        "linking: overrides are validated one at a time and nothing re-links a replaced instance",
        ADDON_LINKING,
        "    for collection in collections:\n"
        "        _refuse_unoverridable(collection)\n"
        "    unlinked: list[tuple[object, object]] = []\n"
        "    reports = []\n"
        "    try:\n"
        "        for collection in collections:\n"
        "            # Again, just before its own override: overriding a parent overrides every collection\n"
        "            # inside it, so a nested request would otherwise build a second copy (measured, section K).\n"
        "            _refuse_unoverridable(collection)\n"
        "            reports.append(_override_hierarchy(collection, scene, unlinked, detail=detail))\n"
        "    except Exception:\n"
        "        for parent, child in reversed(unlinked):\n"
        "            if not _has_child(parent, child):\n"
        "                parent.children.link(child)  # type: ignore[attr-defined]\n"
        "        raise\n"
        "    return reports\n",
        "    unlinked: list[tuple[object, object]] = []\n"
        "    reports = []\n"
        "    for collection in collections:\n"
        "        _refuse_unoverridable(collection)\n"
        "        reports.append(_override_hierarchy(collection, scene, unlinked, detail=detail))\n"
        "    return reports\n",
        (
            f"{LKT}::test_a_refused_multi_collection_override_keeps_the_existing_placement",
            f"{LKT}::test_a_later_override_failure_restores_the_instances_earlier_overrides_replaced",
        ),
    ),
    Revert(
        "linking: a later override failure does not re-link the instances earlier ones replaced",
        ADDON_LINKING,
        "            if not _has_child(parent, child):\n"
        "                parent.children.link(child)  # type: ignore[attr-defined]\n",
        "            pass\n",
        (f"{LKT}::test_a_later_override_failure_restores_the_instances_earlier_overrides_replaced",),
    ),
    Revert(
        "linking: a datablock the file no longer holds is listed as present",
        ADDON_LINKING,
        '        "is_missing": bool(getattr(datablock, "is_missing", False)),\n',
        '        "is_missing": False,\n',
        (
            f"{LKT}::test_datablocks_the_file_no_longer_holds_are_reported_missing_with_a_warning[reload_library]",
            f"{LKT}::test_datablocks_the_file_no_longer_holds_are_reported_missing_with_a_warning[relocate_library]",
        ),
    ),
    Revert(
        "linking: no warning when a reload leaves placeholders",
        ADDON_LINKING,
        "    if not missing:\n        return {}\n",
        "    return {}\n",
        (
            f"{LKT}::test_datablocks_the_file_no_longer_holds_are_reported_missing_with_a_warning[reload_library]",
            f"{LKT}::test_datablocks_the_file_no_longer_holds_are_reported_missing_with_a_warning[relocate_library]",
        ),
    ),
    Revert(
        "linking: the missing-datablock warning is sent when nothing is missing",
        ADDON_LINKING,
        "    if not missing:\n        return {}\n",
        "",
        (f"{LKT}::test_a_reload_that_finds_everything_carries_no_warning",),
    ),
    Revert(
        "linking: an indirect library can be relocated",
        ADDON_LINKING,
        (
            "        if is_indirect_library(library):\n"
            "            raise ValueError(\n"
            '                "that library is indirect'
        ),
        ('        if False:\n            raise ValueError(\n                "that library is indirect'),
        (f"{LKT}::test_relocate_refuses_an_indirect_library",),
    ),
    Revert(
        "linking: create_override does not check use_scripts_auto_execute",
        ADDON_LINKING,
        '        refuse_scripts_auto_execute("create_override")\n',
        "",
        (
            f"{LKT}::test_create_override_refuses_while_scripts_auto_execute_is_on[on]",
            f"{LKT}::test_create_override_refuses_while_scripts_auto_execute_is_on[unreadable]",
        ),
    ),
    Revert(
        "file paths: a quoted library name is not reduced before path detection",
        ADDON_FILE_PATHS,
        "    text = _QUOTED_LIBRARY_NAME.sub(_leaf_library_name, text)\n",
        "",
        (
            f"{FPT}::test_sanitizer_reduces_a_library_name_holding_an_absolute_path_to_its_leaf",
            *(
                f"{FPT}::test_sanitizer_reduces_every_quoted_library_name_shape_to_its_leaf[{case}]"
                for case in ("relocate-indirect", "delete-indirect", "from-library")
            ),
        ),
    ),
    Revert(
        "file paths: a quoted library name is kept whole instead of reduced to its leaf",
        ADDON_FILE_PATHS,
        "{client_safe_name_leaf(match['name'])}",
        "{match['name']}",
        (
            f"{FPT}::test_sanitizer_reduces_a_library_name_holding_an_absolute_path_to_its_leaf",
            f"{FPT}::test_sanitizer_reduces_a_library_name_without_touching_the_filesystem",
            *(
                f"{FPT}::test_sanitizer_reduces_every_quoted_library_name_shape_to_its_leaf[{case}]"
                for case in ("relocate-indirect", "delete-indirect", "from-library")
            ),
        ),
    ),
    # --- nested override requests, and reducing a library name ---
    Revert(
        "linking: a nested request is not re-checked before each override, so a child is overridden twice",
        ADDON_LINKING,
        "            _refuse_unoverridable(collection)\n            reports.append(",
        "            reports.append(",
        (f"{LKT}::test_a_nested_request_is_refused_before_it_overrides_a_collection_twice",),
    ),
    Revert(
        "file paths: the quoted-name match stops at a newline",
        ADDON_FILE_PATHS,
        '>]))", re.DOTALL\n',
        '>]))"\n',
        (f"{FPT}::test_sanitizer_reduces_a_library_name_containing_a_newline",),
    ),
    # `file paths: a library name is reduced with the isdir-checking leaf rule` stood here.
    # `client_safe_leaf` stats nothing any more - the caller that holds the path passes
    # `is_directory` in - so swapping the leaf rules no longer probes the filesystem and the
    # row could not make its node fail. The node moved to the row above, which still can.
    Revert(
        "linking: an absolute library name is not a known path",
        ADDON_LINKING,
        "    return _absolute((raw, expanded, canonical_path(expanded), name))\n",
        "    return _absolute((raw, expanded, canonical_path(expanded)))\n",
        (f"{LKT}::test_an_absolute_library_name_is_a_known_path_so_no_relative_tail_survives",),
    ),
    # --- nested collection overrides, and nothing stats Library.name ---
    Revert(
        "linking: a collection whose inner collection is already overridden is overridden again",
        ADDON_LINKING,
        "    if inner_overrides:\n",
        "    if False:\n",
        tuple(
            f"{LKT}::test_overriding_a_parent_whose_inner_collection_is_already_overridden_is_refused[{route}]"
            for route in ("create_override", "link_canon_library")
        ),
    ),
    Revert(
        "linking: a request naming a collection and one inside it is not refused up front",
        ADDON_LINKING,
        "    _refuse_nested_requests(collections)\n",
        "",
        (f"{LKT}::test_a_request_naming_a_collection_and_one_inside_it_is_refused_up_front",),
    ),
    Revert(
        "linking: the library summary stats Library.name",
        ADDON_BLEND_FILES,
        '        "name": client_safe_name_leaf(getattr(library, "name", "")),',
        '        "name": _reverted_statting_name(getattr(library, "name", "")),',
        (f"{LKT}::test_library_names_are_reduced_without_touching_the_filesystem",),
        also=REVERTED_STATTING_NAME,
    ),
    Revert(
        "linking: relocate stats Library.name for name_before",
        ADDON_LINKING,
        "        name_before = client_safe_name_leaf(library.name)",
        "        name_before = _reverted_statting_name(library.name)",
        (f"{LKT}::test_library_names_are_reduced_without_touching_the_filesystem",),
        also=REVERTED_STATTING_NAME,
    ),
    Revert(
        "linking: relocate stats Library.name for name_after",
        ADDON_LINKING,
        '            "name_after": client_safe_name_leaf(library.name),',
        '            "name_after": _reverted_statting_name(library.name),',
        (f"{LKT}::test_library_names_are_reduced_without_touching_the_filesystem",),
        also=REVERTED_STATTING_NAME,
    ),
    Revert(
        "linking: a refusal's candidate list stats Library.name",
        # `candidates.display_name`, imported by linking as `_display_name`.
        ADDON_CANDIDATES,
        "        return client_safe_name_leaf(name)\n",
        "        return _reverted_statting_name(name)\n",
        (f"{LKT}::test_library_names_are_reduced_without_touching_the_filesystem",),
        also=REVERTED_STATTING_NAME,
    ),
]
