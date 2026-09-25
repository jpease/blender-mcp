"""
Rows guarding the rig's --work-dir, frame bounds and teardown, and the container's.

Label prefixes: `rig:`, `entrypoint:`.
"""

from .common import DOCKT, ENTRYPOINT, RIG, RIGT, Revert

ROWS: list[Revert] = [
    # --- the rig's --work-dir, its frame bounds, and its teardown ---
    Revert(
        "rig: --work-dir is no longer resolved, so the rig reports a path it is not writing to",
        RIG,
        "        _execute(arguments, arguments.work_dir.expanduser().resolve())",
        "        _execute(arguments, arguments.work_dir.expanduser())",
        (f"{RIGT}::test_a_symlinked_work_dir_is_resolved_before_anything_is_claimed",),
    ),
    Revert(
        "rig: the unreachable symlink refusal is restored, now reachable and refusing valid dirs",
        RIG,
        "    if work_dir.exists() and not work_dir.is_dir():",
        "    if work_dir.is_symlink():\n"
        '        raise RigError(f"--work-dir {work_dir} is a symlink; point it at a real directory.")\n'
        "    if work_dir.exists() and not work_dir.is_dir():",
        (
            f"{RIGT}::test_a_symlinked_work_dir_is_claimed_through_to_the_directory_it_points_at",
            f"{RIGT}::test_foreign_data_behind_a_symlinked_work_dir_is_still_refused",
        ),
    ),
    Revert(
        "rig: a timed-out command escapes as a bare TimeoutError naming neither command nor port",
        RIG,
        "        except TimeoutError as expiry:\n"
        "            raise RigError(self._unanswered_message(request)) from expiry\n",
        "",
        (f"{RIGT}::test_a_command_that_never_came_back_is_reported_as_possibly_still_running",),
    ),
    Revert(
        "rig: the frame timeout bounds each recv() again, so a dribbling peer is read for ever",
        RIG,
        "        remaining = deadline - time.monotonic()\n"
        "        if remaining <= 0:\n"
        '            raise TimeoutError(f"no complete frame within {timeout:g}s ({len(buffer)} bytes received)")\n'
        "        sock.settimeout(remaining)",
        "        sock.settimeout(timeout)",
        (f"{RIGT}::test_one_frame_is_bounded_as_a_whole_not_one_recv_at_a_time",),
    ),
    Revert(
        "rig: the launch and its reader move back outside the try, orphaning Blender on a reader failure",
        RIG,
        "        blender = _launch_blender(work_dir, port, nonce, blender_scripts)\n"
        "        drain = _OutputDrain(blender, log_path, abandoned)\n"
        "        drain.start()",
        "        drain.start()",
        (f"{RIGT}::test_a_launched_blender_is_stopped_even_if_its_reader_cannot_be_constructed",),
        also="",
    ),
    Revert(
        "rig: join() is intolerant of a reader that never started, masking the real cause",
        RIG,
        "        if not self._started:\n            return\n",
        "",
        (f"{RIGT}::test_teardown_tolerates_a_reader_that_never_started",),
    ),
    # --- the container's teardown and its readiness probe ---
    Revert(
        "entrypoint: teardown has no SIGKILL escalation, so a wedged child blocks it for ever",
        ENTRYPOINT,
        "        kill -KILL $blender_pid $mcp_pid $xvfb_pid $readiness_pid 2>/dev/null || true",
        "        : no escalation",
        (f"{DOCKT}::test_teardown_finishes_even_when_a_child_ignores_sigterm",),
    ),
    Revert(
        "entrypoint: the grace period is always spent, even when every child has already gone",
        ENTRYPOINT,
        '    kill -TERM "$watchdog_pid" 2>/dev/null || true',
        "    : leave the watchdog running",
        (f"{DOCKT}::test_teardown_costs_nothing_when_every_child_has_already_gone",),
    ),
    Revert(
        "entrypoint: the readiness probe runs in the foreground again, deferring docker stop",
        ENTRYPOINT,
        "    blender_readiness_probe &",
        "    blender_readiness_probe",
        (f"{DOCKT}::test_a_stop_signal_during_the_readiness_wait_is_handled_at_once",),
    ),
]
