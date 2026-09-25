"""
Rows guarding `open_shot`, `save_shot` and `reset_session`.

Label prefix: `file lifecycle:`.
"""

from .common import (
    ADDON_BLEND_FILES,
    ADDON_FILE_LIFECYCLE,
    ADDON_FILE_PATHS,
    ADDON_SERVER_CORE,
    FLT,
    SERVER_CORE_TOOL,
    Revert,
)

ROWS: list[Revert] = [
    # --- open_shot, save_shot, reset_session ---
    Revert(
        "file lifecycle: open_mainfile inherits use_scripts instead of passing False",
        ADDON_FILE_LIFECYCLE,
        "bpy.ops.wm.open_mainfile(filepath=canonical, load_ui=load_ui, use_scripts=False)",
        "bpy.ops.wm.open_mainfile(filepath=canonical, load_ui=load_ui)",
        (f"{FLT}::test_open_shot_passes_use_scripts_false_explicitly",),
    ),
    Revert(
        "file lifecycle: open_mainfile inherits load_ui (the operator default is True)",
        ADDON_FILE_LIFECYCLE,
        "bpy.ops.wm.open_mainfile(filepath=canonical, load_ui=load_ui, use_scripts=False)",
        "bpy.ops.wm.open_mainfile(filepath=canonical, use_scripts=False)",
        (f"{FLT}::test_open_shot_passes_load_ui_false_explicitly_by_default",),
    ),
    Revert(
        "file lifecycle: use_scripts exposed as an open_shot parameter",
        ADDON_FILE_LIFECYCLE,
        "self, filepath: object, load_ui: object = False, discard_unsaved: object = False\n",
        "self, filepath: object, load_ui: object = False, discard_unsaved: object = False, "
        "use_scripts: object = False\n",
        (f"{FLT}::test_no_file_command_takes_a_use_scripts_parameter",),
    ),
    Revert(
        "file lifecycle: a server-side tool schema names use_scripts",
        SERVER_CORE_TOOL,
        None,
        "\n# use_scripts\n",
        (f"{FLT}::test_use_scripts_appears_in_no_server_side_schema",),
    ),
    Revert(
        "file lifecycle: open_shot destroys unsaved work without asking",
        ADDON_FILE_LIFECYCLE,
        "        if dirty and not discard_unsaved:\n",
        "        if False:\n",
        (f"{FLT}::test_open_shot_refuses_a_dirty_session_without_discard_unsaved",),
    ),
    Revert(
        "file lifecycle: discard_unsaved is ignored, so a dirty session can never be replaced",
        ADDON_FILE_LIFECYCLE,
        "        if dirty and not discard_unsaved:\n",
        "        if dirty:\n",
        (f"{FLT}::test_open_shot_opens_a_dirty_session_when_discard_unsaved_is_true",),
    ),
    Revert(
        "file lifecycle: use_scripts_auto_execute is not checked before the load",
        ADDON_FILE_LIFECYCLE,
        "        refuse_scripts_auto_execute()\n",
        "",
        (
            f"{FLT}::test_open_shot_refuses_while_scripts_auto_execute_is_enabled",
            f"{FLT}::test_open_shot_refuses_when_the_auto_execute_preference_cannot_be_read",
        ),
    ),
    Revert(
        "file lifecycle: an unreadable auto-execute preference is read as off (fail open)",
        ADDON_BLEND_FILES,
        'if getattr(filepaths, "use_scripts_auto_execute", True) is not False:',
        'if getattr(filepaths, "use_scripts_auto_execute", False) is True:',
        (f"{FLT}::test_open_shot_refuses_when_the_auto_execute_preference_cannot_be_read",),
    ),
    Revert(
        "file lifecycle: the auto-execute check refuses whatever the preference says",
        ADDON_BLEND_FILES,
        'if getattr(filepaths, "use_scripts_auto_execute", True) is not False:',
        "if True:",
        (f"{FLT}::test_open_shot_proceeds_while_scripts_auto_execute_is_disabled",),
    ),
    Revert(
        "file lifecycle: resolve_blend_path skipped, the raw path reaches the operator",
        ADDON_BLEND_FILES,
        (
            "    return resolve_blend_path(\n"
            "        _expand_blender_relative(raw),\n"
            "        roots=configured_file_roots(),\n"
            "        must_exist=must_exist,\n"
            "        create_directories=create_directories,\n"
            "    )\n"
        ),
        (
            "    expanded = _expand_blender_relative(raw)\n"
            '    if isinstance(expanded, str) and expanded.strip() and "\\x00" not in expanded:\n'
            "        enforce_roots(expanded, configured_file_roots())\n"
            "    return str(expanded)\n"
        ),
        (
            *(
                f"{FLT}::test_open_shot_validates_the_path_before_any_operator_runs[{case}]"
                for case in ("missing", "directory", "non-blend", "bad-magic", "empty")
            ),
            f"{FLT}::test_each_file_command_is_answered_exactly_once_through_the_drain_loop[open refused]",
        ),
        ("\n\nfrom ..file_paths import enforce_roots\n"),
    ),
    Revert(
        "file lifecycle: a // path in an unsaved session resolves against the process CWD",
        ADDON_BLEND_FILES,
        ('    if not bpy.data.filepath:\n        raise ValueError(\n            "a Blender-relative'),
        ('    if False:\n        raise ValueError(\n            "a Blender-relative'),
        (
            f"{FLT}::test_open_shot_refuses_a_blender_relative_path_in_an_unsaved_session",
            f"{FLT}::test_save_shot_refuses_a_blender_relative_path_in_an_unsaved_session",
        ),
    ),
    Revert(
        "file lifecycle: a // path is never expanded",
        ADDON_BLEND_FILES,
        "    return bpy.path.abspath(raw)\n",
        "    return raw\n",
        (f"{FLT}::test_open_shot_expands_a_blender_relative_path_against_the_open_file",),
    ),
    Revert(
        "file lifecycle: file roots not enforced on open or save",
        ADDON_BLEND_FILES,
        "        roots=configured_file_roots(),\n",
        "        roots=[],\n",
        (
            f"{FLT}::test_open_shot_enforces_the_configured_roots",
            f"{FLT}::test_open_shot_refuses_outside_the_roots_before_saying_whether_the_file_exists",
            f"{FLT}::test_save_shot_enforces_the_roots_for_an_explicit_target_and_for_the_open_file",
            f"{FLT}::test_save_shot_creates_no_directory_outside_the_roots_or_on_a_refusal",
        ),
    ),
    Revert(
        "file lifecycle: roots checked after the file checks, so existence leaks outside the roots",
        ADDON_FILE_PATHS,
        (
            "    resolved = canonical_path(raw)\n"
            "    try:\n"
            "        enforce_roots(resolved, roots)\n"
            "    except PathOutsideRootsError as refusal:\n"
            "        if os.path.isabs(os.path.expanduser(raw)):\n"
            "            raise\n"
            '        raise PathOutsideRootsError(f"{refusal}{_RELATIVE_ROOTS_REFUSAL}") from None\n'
            "    if not (_has_blend_suffix(raw) and _has_blend_suffix(resolved)):\n"
            '        raise ValueError("path must name a file ending in .blend")\n'
            "    if must_exist:\n"
            "        _require_blend_file(resolved)\n"
            "    else:\n"
            "        _require_save_target(resolved, create_directories=create_directories)\n"
        ),
        (
            "    resolved = canonical_path(raw)\n"
            "    if not (_has_blend_suffix(raw) and _has_blend_suffix(resolved)):\n"
            '        raise ValueError("path must name a file ending in .blend")\n'
            "    if must_exist:\n"
            "        _require_blend_file(resolved)\n"
            "    else:\n"
            "        _require_save_target(resolved, create_directories=create_directories)\n"
            "    enforce_roots(resolved, roots)\n"
        ),
        (f"{FLT}::test_open_shot_refuses_outside_the_roots_before_saying_whether_the_file_exists",),
    ),
    Revert(
        "file lifecycle: the in-place save target is not held to the roots",
        ADDON_FILE_LIFECYCLE,
        "        canonical = checked_blend_path(requested, must_exist=False, create_directories=create_directories)\n",
        (
            "        canonical = (\n"
            "            str(requested)\n"
            "            if in_place\n"
            "            else checked_blend_path(requested, must_exist=False, create_directories=create_directories)\n"
            "        )\n"
        ),
        (f"{FLT}::test_save_shot_enforces_the_roots_for_an_explicit_target_and_for_the_open_file",),
    ),
    Revert(
        "file lifecycle: open_mainfile's RuntimeError reaches the client raw",
        ADDON_FILE_LIFECYCLE,
        'raise RuntimeError(operator_failure_message("open_shot", exc, (filepath, canonical))) from exc',
        "raise RuntimeError(str(exc)) from exc",
        (
            f"{FLT}::test_a_runtime_error_from_open_mainfile_is_a_clean_error_response",
            f"{FLT}::test_each_file_command_is_answered_exactly_once_through_the_drain_loop[open raises]",
        ),
    ),
    Revert(
        "file lifecycle: the sanitizer is not given the known paths (structural detection only)",
        ADDON_BLEND_FILES,
        "sanitize_blender_error(exc, known_paths=known)",
        "sanitize_blender_error(exc)",
        (f"{FLT}::test_the_known_path_closes_what_structural_detection_leaves_behind",),
    ),
    Revert(
        "file lifecycle: a save operator's RuntimeError reaches the client raw",
        ADDON_FILE_LIFECYCLE,
        (
            '        message = operator_failure_message("save_shot", exc, (request.requested, request.canonical))\n'
            "        raise RuntimeError(message) from exc\n"
        ),
        "        raise RuntimeError(str(exc)) from exc\n",
        (
            f"{FLT}::test_a_runtime_error_from_a_save_operator_is_a_clean_error_response[save_as_mainfile]",
            f"{FLT}::test_a_runtime_error_from_a_save_operator_is_a_clean_error_response[save_mainfile]",
            f"{FLT}::test_each_file_command_is_answered_exactly_once_through_the_drain_loop[save raises]",
        ),
    ),
    Revert(
        "file lifecycle: the reset operator's RuntimeError reaches the client raw",
        ADDON_FILE_LIFECYCLE,
        'raise RuntimeError(operator_failure_message("reset_session", exc, (previous,))) from exc',
        "raise RuntimeError(str(exc)) from exc",
        (
            f"{FLT}::test_a_runtime_error_from_the_reset_operator_is_a_clean_error_response",
            f"{FLT}::test_each_file_command_is_answered_exactly_once_through_the_drain_loop[reset raises]",
        ),
    ),
    Revert(
        "file lifecycle: a swap result does not tell the client to re-handshake",
        ADDON_FILE_LIFECYCLE,
        '            "rehandshake_required": True,\n',
        '            "rehandshake_required": False,\n',
        (
            f"{FLT}::test_open_shot_reports_the_new_session_and_asks_for_a_rehandshake",
            f"{FLT}::test_reset_session_reads_the_empty_factory_startup_file_and_never_factory_settings",
            f"{FLT}::test_a_swap_that_landed_is_still_reported_as_a_success_when_its_report_fails",
        ),
    ),
    Revert(
        "file lifecycle: capabilities_changed is hard-coded False",
        ADDON_FILE_LIFECYCLE,
        'report["capabilities_changed"] = self._capability_names() != capabilities_before',
        'report["capabilities_changed"] = False',
        (f"{FLT}::test_open_shot_reports_that_the_capability_set_followed_the_file",),
    ),
    Revert(
        "file lifecycle: a failing post-swap report turns a landed swap into an error",
        ADDON_FILE_LIFECYCLE,
        ('        except _POST_SWAP_READ_ERRORS as exc:\n            print(f"BlenderMCP: the swap completed'),
        ('        except ZeroDivisionError as exc:\n            print(f"BlenderMCP: the swap completed'),
        (f"{FLT}::test_a_swap_that_landed_is_still_reported_as_a_success_when_its_report_fails",),
    ),
    Revert(
        "file lifecycle: flags are coerced with bool(), so the string 'true' confirms",
        ADDON_BLEND_FILES,
        (
            "    if not isinstance(value, bool):\n"
            '        raise ValueError(f"{name} must be true or false")\n'
            "    return value\n"
        ),
        "    return bool(value)\n",
        tuple(
            f"{FLT}::test_a_flag_that_is_not_a_real_bool_is_refused[{case}]"
            for case in (
                "open_shot-load_ui",
                "open_shot-discard_unsaved",
                "save_shot-compress",
                "save_shot-relative_remap",
                "save_shot-confirm_overwrite",
                "reset_session-confirm",
            )
        ),
    ),
    Revert(
        "file lifecycle: save inherits relative_remap (save_as_mainfile's default is True)",
        ADDON_FILE_LIFECYCLE,
        "operator(filepath=request.canonical, compress=request.compress, relative_remap=request.relative_remap)",
        "operator(filepath=request.canonical, compress=request.compress)",
        (
            f"{FLT}::test_save_shot_passes_compress_and_relative_remap_false_explicitly",
            f"{FLT}::test_save_shot_in_place_uses_save_mainfile_with_explicit_arguments",
        ),
    ),
    Revert(
        "file lifecycle: save inherits compress (use_file_compression wins at factory settings)",
        ADDON_FILE_LIFECYCLE,
        "operator(filepath=request.canonical, compress=request.compress, relative_remap=request.relative_remap)",
        "operator(filepath=request.canonical, relative_remap=request.relative_remap)",
        (
            f"{FLT}::test_save_shot_passes_compress_and_relative_remap_false_explicitly",
            f"{FLT}::test_save_shot_in_place_uses_save_mainfile_with_explicit_arguments",
        ),
    ),
    Revert(
        "file lifecycle: an explicit compress / relative_remap opt-in is dropped",
        ADDON_FILE_LIFECYCLE,
        "operator(filepath=request.canonical, compress=request.compress, relative_remap=request.relative_remap)",
        "operator(filepath=request.canonical, compress=False, relative_remap=False)",
        (f"{FLT}::test_save_shot_forwards_an_explicit_opt_in",),
    ),
    Revert(
        "file lifecycle: no overwrite pre-check, check_existing left to guard (it does not)",
        ADDON_FILE_LIFECYCLE,
        "        if exists and not confirm_overwrite:\n",
        "        if False:\n",
        (
            f"{FLT}::test_saving_over_an_existing_file_without_confirmation_calls_no_operator",
            f"{FLT}::test_save_shot_in_place_needs_confirmation_because_it_overwrites_the_file_on_disk",
            f"{FLT}::test_each_file_command_is_answered_exactly_once_through_the_drain_loop[save refused]",
        ),
    ),
    Revert(
        "file lifecycle: confirm_overwrite is ignored, so an existing file can never be replaced",
        ADDON_FILE_LIFECYCLE,
        "        if exists and not confirm_overwrite:\n",
        "        if exists:\n",
        (
            f"{FLT}::test_saving_over_an_existing_file_with_confirmation_writes_it",
            f"{FLT}::test_save_shot_in_place_uses_save_mainfile_with_explicit_arguments",
            f"{FLT}::test_a_runtime_error_from_a_save_operator_is_a_clean_error_response[save_mainfile]",
        ),
    ),
    Revert(
        "file lifecycle: an in-place save on a never-saved session is not refused up front",
        ADDON_FILE_LIFECYCLE,
        "        if in_place and not bpy.data.filepath:\n",
        "        if False:\n",
        (f"{FLT}::test_save_shot_in_place_on_an_unsaved_session_is_refused_with_an_actionable_message",),
    ),
    Revert(
        "file lifecycle: an in-place save goes through save_as_mainfile",
        ADDON_FILE_LIFECYCLE,
        "    operator = bpy.ops.wm.save_mainfile if request.in_place else bpy.ops.wm.save_as_mainfile",
        "    operator = bpy.ops.wm.save_as_mainfile",
        (
            f"{FLT}::test_save_shot_in_place_uses_save_mainfile_with_explicit_arguments",
            f"{FLT}::test_a_runtime_error_from_a_save_operator_is_a_clean_error_response[save_mainfile]",
        ),
    ),
    Revert(
        "file lifecycle: reset_session runs without confirm",
        ADDON_FILE_LIFECYCLE,
        '        if not require_bool("confirm", confirm):\n',
        '        if not require_bool("confirm", True):\n',
        (
            f"{FLT}::test_reset_session_without_confirm_is_refused",
            f"{FLT}::test_a_flag_that_is_not_a_real_bool_is_refused[reset_session-confirm]",
            f"{FLT}::test_each_file_command_is_answered_exactly_once_through_the_drain_loop[reset refused]",
        ),
    ),
    Revert(
        "file lifecycle: reset_session uses read_factory_settings (unregisters every add-on)",
        ADDON_FILE_LIFECYCLE,
        "bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True, load_ui=False)",
        "bpy.ops.wm.read_factory_settings(use_empty=True)",
        (
            f"{FLT}::test_reset_session_reads_the_empty_factory_startup_file_and_never_factory_settings",
            f"{FLT}::test_a_runtime_error_from_the_reset_operator_is_a_clean_error_response",
            f"{FLT}::test_each_file_command_is_answered_exactly_once_through_the_drain_loop[reset raises]",
        ),
    ),
    Revert(
        "file lifecycle: the three commands are not in the dispatch table",
        ADDON_SERVER_CORE,
        '        "open_shot": CommandSpec(session_swap=True, indeterminate_safe=True),\n'
        '        "save_shot": CommandSpec(tick_ending=True),\n'
        '        "reset_session": CommandSpec(session_swap=True, indeterminate_safe=True),\n',
        "",
        (
            f"{FLT}::test_the_file_commands_are_dispatchable_and_advertised_beside_get_session_info",
            *(
                f"{FLT}::test_each_file_command_is_answered_exactly_once_through_the_drain_loop[{case}]"
                for case in ("open ok", "save ok", "reset ok")
            ),
        ),
    ),
    # --- save_shot: the dirty flag, relative-link warnings, the temp name ---
    Revert(
        "file lifecycle: the drain tick does not end after save_shot, so an edit behind it loses its dirty flag",
        ADDON_SERVER_CORE,
        '            if self.command_spec(command.get("type")).tick_ending:\n                break\n',
        "",
        (f"{FLT}::test_the_drain_tick_ends_after_a_save_so_a_queued_edit_runs_after_blender_clears_the_dirty_flag",),
    ),
    Revert(
        "file lifecycle: save_shot reports a same-tick is_dirty that Blender has not cleared yet",
        ADDON_FILE_LIFECYCLE,
        ('        "relative_remap": request.relative_remap,\n        "session_id": session["session_id"],\n'),
        (
            '        "relative_remap": request.relative_remap,\n'
            '        "is_dirty": bool(bpy.data.is_dirty),\n'
            '        "session_id": session["session_id"],\n'
        ),
        (f"{FLT}::test_save_shot_does_not_report_a_dirty_flag_blender_has_not_cleared_yet",),
    ),
    Revert(
        "file lifecycle: no warning for //-relative links a save to a new directory breaks",
        ADDON_FILE_LIFECYCLE,
        "        broken_links = _unresolvable_relative_paths(request.canonical, request.relative_remap)\n",
        "        broken_links = 0\n",
        (
            f"{FLT}::test_saving_to_another_directory_warns_about_relative_links_that_will_not_resolve",
            f"{FLT}::test_a_relative_image_path_counts_as_an_external_path_that_will_not_resolve",
            f"{FLT}::test_an_indirect_library_is_not_counted_because_blender_rederives_it_from_its_parent",
        ),
    ),
    Revert(
        "file lifecycle: the relative-link warning ignores the directory and the remap flag",
        ADDON_FILE_LIFECYCLE,
        "    if relative_remap or not current:\n        return 0\n"
        "    if os.path.dirname(canonical) == os.path.dirname(canonical_path(current)):\n        return 0\n",
        "",
        tuple(
            f"{FLT}::test_no_relative_link_warning_when_the_links_still_resolve[{case}]"
            for case in ("same directory", "relative_remap", "in place")
        ),
    ),
    Revert(
        "file lifecycle: a planted <target>@ is not checked, so the save follows it out of the roots",
        ADDON_FILE_LIFECYCLE,
        "        _refuse_a_leftover_temp_save(canonical)\n",
        "",
        tuple(
            f"{FLT}::test_a_leftover_temp_save_name_beside_the_target_refuses_the_save[{place}-{kind}]"
            for place in ("explicit", "in place")
            for kind in ("file", "dangling symlink", "directory")
        ),
    ),
    Revert(
        "file lifecycle: the temp-name check uses exists(), which a dangling symlink passes",
        ADDON_FILE_LIFECYCLE,
        '        os.lstat(f"{canonical}@")\n    except FileNotFoundError:\n        return\n',
        '        if not os.path.exists(f"{canonical}@"):\n'
        "            return\n"
        "    except FileNotFoundError:\n"
        "        return\n",
        tuple(
            f"{FLT}::test_a_leftover_temp_save_name_beside_the_target_refuses_the_save[{place}-dangling symlink]"
            for place in ("explicit", "in place")
        ),
    ),
    # --- save_shot: temp-name lstat failures, and which relative paths are counted ---
    Revert(
        "file lifecycle: an lstat failure other than not-found reads as a clear temp name",
        ADDON_FILE_LIFECYCLE,
        "    except OSError as exc:\n        raise ValueError(\n",
        "    except OSError as exc:\n        return\n        raise ValueError(\n",
        tuple(
            f"{FLT}::test_a_temp_save_name_that_cannot_be_checked_refuses_the_save[{place}-{errno_id}]"
            for errno_id in ("EACCES", "ENAMETOOLONG")
            for place in ("explicit", "in place")
        ),
    ),
    Revert(
        "file lifecycle: an occupied temp name is not said to be possibly left by an interrupted save",
        ADDON_FILE_LIFECYCLE,
        'already exists beside the target, possibly "\n        "left by an interrupted save;',
        'already exists beside the target; "\n        "remove it now;',
        (f"{FLT}::test_an_occupied_temp_save_name_says_it_may_be_left_by_an_interrupted_save",),
    ),
    Revert(
        "file lifecycle: only libraries are counted, so a relative image path breaks with no warning",
        ADDON_FILE_LIFECYCLE,
        "        for path in bpy.utils.blend_paths(absolute=False, packed=False, local=True)\n",
        "        for path in (library.filepath for library in bpy.data.libraries)\n",
        (
            f"{FLT}::test_a_relative_image_path_counts_as_an_external_path_that_will_not_resolve",
            f"{FLT}::test_an_indirect_library_is_not_counted_because_blender_rederives_it_from_its_parent",
        ),
    ),
    Revert(
        "file lifecycle: indirect libraries are counted although they re-resolve from their parent",
        ADDON_FILE_LIFECYCLE,
        "for library in bpy.data.libraries if is_indirect_library(library)",
        "for library in bpy.data.libraries if False",
        (f"{FLT}::test_an_indirect_library_is_not_counted_because_blender_rederives_it_from_its_parent",),
    ),
]
