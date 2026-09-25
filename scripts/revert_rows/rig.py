"""
Rows guarding the GUI rig.

Which Blender it launches, which process answers the port, what it may delete and
overwrite, its own liveness, and the boundary it has to stay outside of.

Label prefixes: `rig:`, `rig control:`, `boundary:`.
"""

from .common import PACKAGE_INIT, PYPROJECT, RIG, RIGT, SCRIPTS_INIT, Revert

# `_bare_connect` exists only for the readiness revert; it is appended with it.
BARE_CONNECT = '''

def _bare_connect(port: int) -> bool:
    """
    Reverted readiness: a connect with no round-trip (for the revert matrix only).

    Args:
        port: The port to probe.

    Returns:
        bool: Whether a TCP connection was accepted.

    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False
'''

ROWS: list[Revert] = [
    # --- which Blender, and which configuration, the rig launches ---
    Revert(
        "rig: --factory-startup dropped from the launch",
        RIG,
        '    command = [str(_BLENDER), "--factory-startup", "--python", str(bootstrap)]',
        '    command = [str(_BLENDER), "--python", str(bootstrap)]',
        (f"{RIGT}::test_blender_is_launched_with_factory_startup",),
    ),
    Revert(
        "rig: only BLENDER_USER_SCRIPTS is set",
        RIG,
        '        "BLENDER_USER_RESOURCES": str(work_dir),\n',
        "",
        (f"{RIGT}::test_both_blender_user_resource_roots_point_at_the_work_dir",),
    ),
    Revert(
        "rig: the launched Blender's output roots left unscoped",
        RIG,
        '        "BLENDERMCP_OUTPUT_ROOTS": str(work_dir),\n',
        "",
        (f"{RIGT}::test_the_launched_blender_is_pointed_at_the_work_dir_first",),
    ),
    Revert(
        "rig: TMPDIR left at the user's own, so bpy.app.tempdir escapes the work dir",
        RIG,
        '        "TMPDIR": str(work_dir / "tmp"),\n',
        "",
        (f"{RIGT}::test_blender_s_session_temp_dir_is_redirected_under_the_work_dir",),
    ),
    Revert(
        "rig: PYTHONPATH no longer pruned from the child environment",
        RIG,
        '_PRUNED_ENVIRONMENT_NAMES = frozenset({"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"})',
        '_PRUNED_ENVIRONMENT_NAMES = frozenset({"PYTHONHOME", "PYTHONSTARTUP"})',
        (f"{RIGT}::test_an_inherited_pythonpath_cannot_shadow_the_staged_addon",),
    ),
    Revert(
        "rig: the prune narrowed back to BLENDER_USER_, so BLENDER_SYSTEM_* survives",
        RIG,
        '_PRUNED_ENVIRONMENT_PREFIXES = ("BLENDER",)\n'
        '_PRUNED_ENVIRONMENT_NAMES = frozenset({"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"})',
        '_PRUNED_ENVIRONMENT_PREFIXES = ("BLENDER_USER_", "BLENDERMCP_")\n'
        '_PRUNED_ENVIRONMENT_NAMES = frozenset({"PYTHONPATH"})',
        (f"{RIGT}::test_blender_system_and_python_home_variables_cannot_redirect_the_child",),
    ),
    # --- which process answers the port ---
    Revert(
        "rig: the preflight no longer refuses an occupied port",
        RIG,
        '    try:\n        with socket.create_connection(("127.0.0.1", port), timeout=1.0):\n'
        "            pass\n    except OSError:\n        return\n    raise RigError(",
        "    if True:\n        return\n    raise RigError(",
        (f"{RIGT}::test_the_rig_refuses_a_port_something_is_already_listening_on",),
    ),
    Revert(
        "rig control: the preflight refuses every port",
        RIG,
        '    try:\n        with socket.create_connection(("127.0.0.1", port), timeout=1.0):\n'
        "            pass\n    except OSError:\n        return\n    raise RigError(",
        "    raise RigError(",
        (f"{RIGT}::test_a_free_port_passes_the_preflight",),
    ),
    Revert(
        "rig: the default port back to the addon's own 9876",
        RIG,
        "_DEFAULT_PORT = 0\n",
        "_DEFAULT_PORT = 9876\n",
        (f"{RIGT}::test_the_default_port_is_an_unused_ephemeral_port",),
    ),
    Revert(
        "rig control: _choose_port ignores an explicit --port",
        RIG,
        "    if requested:\n        return requested\n",
        "    if False:\n        return requested\n",
        (f"{RIGT}::test_an_explicit_port_is_honoured",),
    ),
    Revert(
        "rig: the port is not re-checked immediately before Popen",
        RIG,
        "    _require_port_free(port)\n    return subprocess.Popen(",
        "    return subprocess.Popen(",
        (f"{RIGT}::test_the_port_is_rechecked_immediately_before_blender_is_started",),
    ),
    Revert(
        "rig: readiness back to a bare connect",
        RIG,
        "        if _receipt_matches(receipt, launch.nonce, launch.port) and _ping_answers(launch.port):",
        "        if _receipt_matches(receipt, launch.nonce, launch.port) and _bare_connect(launch.port):",
        (f"{RIGT}::test_readiness_requires_a_ping_round_trip_not_a_bare_connect",),
        also=BARE_CONNECT,
    ),
    Revert(
        "rig: the receipt is not checked at all",
        RIG,
        '    try:\n        written = json.loads(receipt.read_text(encoding="utf-8"))\n'
        "    except (OSError, ValueError):\n        return False\n"
        '    return isinstance(written, dict) and written.get("nonce") == nonce and written.get("port") == port',
        "    return True",
        (f"{RIGT}::test_the_readiness_receipt_must_carry_the_rig_s_own_nonce_and_port",),
    ),
    Revert(
        "rig: the receipt's port is recorded but never checked",
        RIG,
        ' and written.get("port") == port',
        "",
        (f"{RIGT}::test_the_readiness_receipt_must_carry_the_rig_s_own_nonce_and_port",),
    ),
    Revert(
        "rig: a swallowed bind failure is left buried in the log tail",
        RIG,
        '    if _BIND_FAILURE_MARKER not in text:\n        return ""',
        '    if True:\n        return ""',
        (f"{RIGT}::test_a_startup_failure_names_a_bind_failure_the_addon_swallowed",),
    ),
    Revert(
        "rig: the log tail is formatted before the reader is joined",
        RIG,
        "            launch.drain.join(_SHUTDOWN_GRACE_SECONDS)\n            raise RigError(",
        "            raise RigError(",
        (f"{RIGT}::test_the_log_reader_is_joined_before_a_failure_quotes_its_log",),
    ),
    # --- what the rig may delete and overwrite ---
    Revert(
        "rig: any --work-dir is claimed, marker or not",
        RIG,
        "        if existing:\n            raise RigError(_foreign_work_dir_message(work_dir, existing))",
        "        if False:\n            raise RigError(_foreign_work_dir_message(work_dir, existing))",
        (f"{RIGT}::test_a_populated_work_dir_the_rig_did_not_create_is_refused",),
    ),
    Revert(
        "rig: a real Blender resources root is no longer recognised as one",
        RIG,
        '_BLENDER_RESOURCE_ENTRIES = ("config", "datafiles", "extensions", "scripts", "userpref.blend")',
        "_BLENDER_RESOURCE_ENTRIES = ()",
        (f"{RIGT}::test_a_work_dir_that_is_a_real_blender_resources_root_is_refused",),
    ),
    Revert(
        "rig: an auto-executing startup/ tree is no longer recognised",
        RIG,
        '_AUTO_EXECUTED_SCRIPT_DIRS = ("startup", "modules")',
        "_AUTO_EXECUTED_SCRIPT_DIRS = ()",
        (f"{RIGT}::test_a_work_dir_holding_auto_executed_scripts_is_refused",),
    ),
    Revert(
        "rig control: the work dir is never marked, so the rig refuses its own",
        RIG,
        '    marker.write_text(_OWNED_MARKER_TEXT, encoding="utf-8")\n\n\ndef _foreign_work_dir_message',
        "\n\ndef _foreign_work_dir_message",
        (f"{RIGT}::test_an_empty_or_rig_created_work_dir_is_claimed",),
    ),
    Revert(
        "rig: the marker no longer guards a directory the rig did not create",
        RIG,
        "    if directory.is_dir() and not marker.is_file():\n        raise RigError(\n"
        '            f"{directory} exists but carries no {_OWNED_MARKER_NAME}, so the rig did not create it "\n'
        '            "and will not write into it. Point --work-dir at an empty or rig-created directory."\n'
        "        )\n",
        "",
        (
            f"{RIGT}::test_staging_refuses_to_delete_an_addons_directory_it_does_not_own",
            f"{RIGT}::test_a_pre_placed_fixture_is_not_silently_overwritten",
        ),
    ),
    Revert(
        "rig: <work-dir>/addons as a regular file is no longer refused",
        RIG,
        "    if directory.exists() and not directory.is_dir():\n"
        '        raise RigError(f"{directory} exists and is not a directory; the rig will not replace it.")\n',
        "",
        (f"{RIGT}::test_an_addons_path_that_is_a_regular_file_is_refused",),
    ),
    Revert(
        "rig control: staging merges into its own previous stage instead of rebuilding it",
        RIG,
        "    if staged.exists():\n        shutil.rmtree(staged)\n",
        "",
        (f"{RIGT}::test_staging_replaces_its_own_previous_stage",),
    ),
    Revert(
        "rig: the whole addons/ tree is removed again, not just blender_mcp",
        RIG,
        "    if staged.exists():\n        shutil.rmtree(staged)\n",
        "    if addons.exists():\n        shutil.rmtree(addons)\n    addons.mkdir(parents=True)\n",
        (f"{RIGT}::test_staging_leaves_other_add_ons_in_its_own_directory_alone",),
    ),
    Revert(
        "rig: a symlinked addons/ directory is no longer refused",
        RIG,
        "    if directory.is_symlink():\n"
        '        raise RigError(f"{directory} is a symlink; refusing to write through it. '
        'Use a --work-dir the rig owns.")\n',
        "",
        (f"{RIGT}::test_staging_refuses_a_symlinked_addons_directory",),
    ),
    Revert(
        "rig: fixtures handed over by their original path",
        RIG,
        "        destination.unlink(missing_ok=True)\n        shutil.copy2(source, destination)\n"
        "        copies[name] = destination",
        "        copies[name] = source",
        (f"{RIGT}::test_fixtures_are_copied_so_a_scenario_cannot_write_through_to_the_original",),
    ),
    Revert(
        "rig: blends/ is created without the rig's marker, so a pre-placed file is overwritten",
        RIG,
        '    staged_dir = _rig_owned_subdirectory(work_dir, "blends")',
        '    staged_dir = work_dir / "blends"\n    staged_dir.mkdir(parents=True, exist_ok=True)',
        (f"{RIGT}::test_a_pre_placed_fixture_is_not_silently_overwritten",),
    ),
    Revert(
        "rig control: every existing fixture destination is refused, reused work dir or not",
        RIG,
        "        destination.unlink(missing_ok=True)\n",
        '        if destination.exists():\n            raise RigError("refusing any existing destination")\n',
        (f"{RIGT}::test_the_rig_replaces_a_fixture_copy_it_made_itself",),
    ),
    Revert(
        "rig: copy2 follows a symlinked fixture destination again",
        RIG,
        "        if destination.is_symlink():\n"
        '            raise RigError(f"{destination} is a symlink; refusing to write through it.")\n',
        "",
        (f"{RIGT}::test_a_symlinked_fixture_destination_is_not_written_through",),
    ),
    Revert(
        "rig: two --blend fixtures may share one name again",
        RIG,
        "        if name in copies:\n"
        '            raise RigError(f"--blend {name} was given twice; the two copies would overwrite each other.")\n',
        "",
        (f"{RIGT}::test_two_fixtures_with_the_same_name_are_refused",),
    ),
    Revert(
        "rig: fixture names unvalidated, so one can escape the work dir",
        RIG,
        "    if not _FIXTURE_NAME.match(name):",
        "    if False:",
        (f"{RIGT}::test_a_fixture_name_cannot_escape_the_work_dir",),
    ),
    Revert(
        "rig: importing the scenario writes __pycache__ beside the caller's file again",
        RIG,
        "    sys.dont_write_bytecode = True\n    spec = importlib_util.spec_from_file_location",
        "    spec = importlib_util.spec_from_file_location",
        (f"{RIGT}::test_importing_a_scenario_leaves_no_pycache_beside_the_caller_s_file",),
    ),
    # --- liveness of the rig itself ---
    Revert(
        "rig: no deadline on the scenario",
        RIG,
        "    if thread.is_alive():\n        abandoned.set()",
        "    if False:\n        abandoned.set()",
        (f"{RIGT}::test_a_scenario_that_never_returns_is_abandoned_at_its_deadline",),
    ),
    Revert(
        "rig control: the scenario's own failure is swallowed",
        RIG,
        "    if raised:\n        raise raised[0]",
        "    return",
        (f"{RIGT}::test_a_failing_scenario_still_reports_its_own_error",),
    ),
    Revert(
        "rig: an abandoned scenario may still send commands",
        RIG,
        "        self._refuse_if_abandoned(command_type)\n        self._sequence += 1",
        "        self._sequence += 1",
        (f"{RIGT}::test_an_abandoned_scenario_cannot_send_another_command",),
    ),
    Revert(
        "rig: the deadline no longer tells the rig it abandoned the scenario",
        RIG,
        "    if thread.is_alive():\n        abandoned.set()\n        raise RigError(",
        "    if thread.is_alive():\n        raise RigError(",
        (f"{RIGT}::test_the_deadline_silences_the_scenario_it_could_not_stop",),
    ),
    Revert(
        "rig: the transcript keeps growing after the verdict",
        RIG,
        "        if not self._abandoned.is_set():\n            print(line, flush=True)",
        "        print(line, flush=True)",
        (f"{RIGT}::test_the_deadline_silences_the_scenario_it_could_not_stop",),
    ),
    Revert(
        "rig: main catches Exception, so sys.exit(0) leaves the rig exiting 0",
        RIG,
        '    except BaseException as failure:\n        detail = ""',
        '    except Exception as failure:\n        detail = ""',
        (f"{RIGT}::test_a_scenario_that_exits_the_process_is_not_reported_as_a_pass",),
    ),
    Revert(
        "rig: Popen used as a context manager, reintroducing an unbounded wait()",
        RIG,
        "        blender = _launch_blender(work_dir, port, nonce, blender_scripts)\n"
        "        drain = _OutputDrain(blender, log_path, abandoned)\n",
        "        with _launch_blender(work_dir, port, nonce, blender_scripts) as blender:\n"
        "            drain = _OutputDrain(blender, log_path, abandoned)\n",
        (f"{RIGT}::test_the_process_is_never_waited_on_without_a_timeout",),
    ),
    Revert(
        "rig: Blender's output no longer drained to exhaustion",
        RIG,
        "            for line in stream:\n                log.write(line)",
        "            for line in [stream.readline()]:\n                log.write(line)",
        (f"{RIGT}::test_blender_s_output_is_drained_to_a_log_instead_of_filling_the_pipe",),
    ),
    Revert(
        "rig: the pipe decodes strictly again",
        RIG,
        '        errors="replace",\n',
        "",
        (f"{RIGT}::test_blender_s_output_is_decoded_leniently",),
    ),
    Revert(
        "rig: the reader dies on the first byte it cannot decode",
        RIG,
        "        try:\n            self._tee()\n"
        "        # Deliberately BaseException: the only outcome worse than losing the log is\n"
        "        # leaving the pipe unread, which deadlocks Blender on its main thread.\n"
        "        except BaseException as failure:\n            self.failure = failure\n"
        "            self._drain_silently()",
        "        self._tee()",
        (f"{RIGT}::test_the_log_reader_survives_output_it_cannot_decode",),
    ),
    Revert(
        "rig: the log reader keeps echoing after the verdict",
        RIG,
        "                if line.startswith(_ECHOED_PREFIXES) and not self._abandoned.is_set():",
        "                if line.startswith(_ECHOED_PREFIXES):",
        (f"{RIGT}::test_the_log_reader_stops_echoing_once_the_scenario_is_abandoned",),
    ),
    Revert(
        "rig: a reader that died is not reported, so Blender is blamed instead",
        RIG,
        "    if drain.failure is not None:\n        problems.append(\n"
        '            f"the rig\'s own log reader died with {type(drain.failure).__name__}: {drain.failure} "\n'
        '            "(the pipe was drained anyway, so this did not hang Blender, but the log may be short)"\n'
        "        )\n",
        "",
        (f"{RIGT}::test_teardown_reports_the_rig_s_own_log_reader_dying",),
    ),
    Revert(
        "rig: a reader still holding the pipe is not reported",
        RIG,
        "    if drain.is_alive():\n        problems.append(\n",
        "    if False:\n        problems.append(\n",
        (f"{RIGT}::test_teardown_reports_a_log_reader_that_outlived_blender",),
    ),
    Revert(
        "rig: failures no longer quote Blender's log",
        RIG,
        '    tail = "\\n".join(lines[-_LOG_TAIL_LINES:])',
        '    tail = ""',
        (f"{RIGT}::test_failures_quote_the_tail_of_that_log",),
    ),
    # --- the boundary the rig has to stay outside of ---
    Revert(
        "boundary: a packaged module references the rig",
        PACKAGE_INIT,
        None,
        "\n# scripts/blender_rig is imported here, which is exactly what must never happen.\n",
        (f"{RIGT}::test_no_packaged_module_references_the_rig",),
    ),
    Revert(
        "boundary: scripts/ becomes a package",
        SCRIPTS_INIT,
        None,
        '"""Make scripts importable, which is the drift this guard exists for."""\n',
        (f"{RIGT}::test_the_rig_is_not_importable_as_part_of_the_package",),
    ),
    Revert(
        "boundary: a packaging root outside src/ would distribute scripts/",
        PYPROJECT,
        'packages = [{ include = "blender_mcp", from = "src" }]',
        'packages = [{ include = "blender_mcp", from = "src" }, { include = "blender_rig", from = "scripts" }]',
        (f"{RIGT}::test_the_rig_is_not_importable_as_part_of_the_package",),
    ),
]
