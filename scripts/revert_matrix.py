"""
Prove every test added by Phase 2 Task 1 fails once the thing it names is broken.

**Why this exists in the repository rather than in a session scratchpad.** The
phase's rubric makes "a test that still passes with its fix reverted" an
automatic critical failure, and four such tests were found in this task alone.
Three of the four hid the same way: the evidence was gathered per *file* rather
than per *test node*, so a sibling assertion failing in the same file read as
"the revert was caught". Every row below therefore names the exact node ids it
expects to fail, and only those nodes are run.

**How to run it:**

    .venv/bin/python scripts/revert_matrix.py           # run the whole matrix
    .venv/bin/python scripts/revert_matrix.py --list    # print the coverage map
    .venv/bin/python scripts/revert_matrix.py --only R2 # rows whose label matches

Each row edits one file in place, runs only its own node ids, and restores the
file in a `finally`. It must be run on a clean tree: a crash between the edit
and the restore leaves a modified working copy, so check `git status` after.
It edits `scripts/blender_rig.py`, the addon, the Docker files and
`pyproject.toml`, so nothing else may be running against the checkout at the
time.

**Coverage is checked, not assumed.** `--list` (and every run) compares the
nodes named here against the nodes pytest actually collects in the five test
files this task added, plus the two tests it added to
`tests/test_addon_manager.py`. A node that is neither covered by a row nor
listed in `NOT_INDIVIDUALLY_FALSIFIABLE`, with a reason, is reported as a gap.
"""

import argparse
import pathlib
import subprocess
import sys

from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parents[1]
RIG = ROOT / "scripts/blender_rig.py"
ADDON_MANAGER = ROOT / "src/blender_mcp/addon_manager.py"
ADDON_OUTPUT_ROOTS = ROOT / "src/blender_mcp/bundled/addon/output_roots.py"
ADDON_SERVER_CORE = ROOT / "src/blender_mcp/bundled/addon/server_core.py"
SERVER_CORE_TOOL = ROOT / "src/blender_mcp/server/tools/core.py"
SERVER_CLI = ROOT / "src/blender_mcp/server/cli.py"
PACKAGE_INIT = ROOT / "src/blender_mcp/__init__.py"
SCRIPTS_INIT = ROOT / "scripts/__init__.py"
PYPROJECT = ROOT / "pyproject.toml"
DOCKERFILE = ROOT / "docker/blender/Dockerfile"
DOCKERIGNORE = ROOT / "docker/blender/Dockerfile.dockerignore"
COMPOSE = ROOT / "docker/blender/docker-compose.yml"
ENTRYPOINT = ROOT / "docker/blender/entrypoint.sh"
HEALTHCHECK = ROOT / "docker/blender/healthcheck.py"
DOCKER_START = ROOT / "docker/blender/start_server.py"

RIGT = "tests/test_blender_rig.py"
DOCKT = "tests/test_docker_rig.py"
ROOTST = "tests/test_output_roots.py"
CORET = "tests/server/tools/test_core.py"
CLIT = "tests/server/test_cli_transport.py"
AMT = "tests/test_addon_manager.py"

# The files Task 1 added outright; every node they collect must be accounted for.
NEW_TEST_FILES = (RIGT, DOCKT, ROOTST, CORET, CLIT)
# The two nodes Task 1 added to a file that already existed.
NEW_NODES_IN_EXISTING_FILES = (
    f"{AMT}::test_handshake_surfaces_writable_output_roots",
    f"{AMT}::test_handshake_defaults_writable_output_roots_when_the_addon_omits_them",
)

# Nodes no single revert can break on their own, with the reason. Keeping these
# named is the point: an omission reads as coverage, which is the defect this
# harness was extended to stop.
NOT_INDIVIDUALLY_FALSIFIABLE: dict[str, str] = {}


@dataclass(frozen=True)
class Revert:
    """
    One reverted behaviour and the test nodes that must notice.

    Attributes:
        label: Human-readable description of what is being undone.
        path: File to edit.
        old: Text to replace; None means "append `new`", creating the file if needed.
        new: Replacement text.
        nodes: Node ids expected to fail, and the only ones run.
        also: Extra text appended to the same file alongside the replacement.

    """

    label: str
    path: pathlib.Path
    old: str | None
    new: str
    nodes: tuple[str, ...]
    also: str = ""


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

REVERTS: list[Revert] = [
    # --- which Blender, and which configuration, the rig launches ---
    Revert(
        "A: --factory-startup dropped from the launch",
        RIG,
        '    command = [str(_BLENDER), "--factory-startup", "--python", str(bootstrap)]',
        '    command = [str(_BLENDER), "--python", str(bootstrap)]',
        (f"{RIGT}::test_blender_is_launched_with_factory_startup",),
    ),
    Revert(
        "R3: only BLENDER_USER_SCRIPTS is set",
        RIG,
        '        "BLENDER_USER_RESOURCES": str(work_dir),\n',
        "",
        (f"{RIGT}::test_both_blender_user_resource_roots_point_at_the_work_dir",),
    ),
    Revert(
        "R5: the launched Blender's output roots left unscoped",
        RIG,
        '        "BLENDERMCP_OUTPUT_ROOTS": str(work_dir),\n',
        "",
        (f"{RIGT}::test_the_launched_blender_is_pointed_at_the_work_dir_first",),
    ),
    Revert(
        "B2: TMPDIR left at the user's own, so bpy.app.tempdir escapes the work dir",
        RIG,
        '        "TMPDIR": str(work_dir / "tmp"),\n',
        "",
        (f"{RIGT}::test_blender_s_session_temp_dir_is_redirected_under_the_work_dir",),
    ),
    Revert(
        "R8: PYTHONPATH no longer pruned from the child environment",
        RIG,
        '_PRUNED_ENVIRONMENT_NAMES = frozenset({"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"})',
        '_PRUNED_ENVIRONMENT_NAMES = frozenset({"PYTHONHOME", "PYTHONSTARTUP"})',
        (f"{RIGT}::test_an_inherited_pythonpath_cannot_shadow_the_staged_addon",),
    ),
    Revert(
        "C2: the prune narrowed back to BLENDER_USER_, so BLENDER_SYSTEM_* survives",
        RIG,
        '_PRUNED_ENVIRONMENT_PREFIXES = ("BLENDER",)\n'
        '_PRUNED_ENVIRONMENT_NAMES = frozenset({"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"})',
        '_PRUNED_ENVIRONMENT_PREFIXES = ("BLENDER_USER_", "BLENDERMCP_")\n'
        '_PRUNED_ENVIRONMENT_NAMES = frozenset({"PYTHONPATH"})',
        (f"{RIGT}::test_blender_system_and_python_home_variables_cannot_redirect_the_child",),
    ),
    # --- which process answers the port ---
    Revert(
        "R1b: the preflight no longer refuses an occupied port",
        RIG,
        '    try:\n        with socket.create_connection(("127.0.0.1", port), timeout=1.0):\n'
        "            pass\n    except OSError:\n        return\n    raise RigError(",
        "    if True:\n        return\n    raise RigError(",
        (f"{RIGT}::test_the_rig_refuses_a_port_something_is_already_listening_on",),
    ),
    Revert(
        "R1b control: the preflight refuses every port",
        RIG,
        '    try:\n        with socket.create_connection(("127.0.0.1", port), timeout=1.0):\n'
        "            pass\n    except OSError:\n        return\n    raise RigError(",
        "    raise RigError(",
        (f"{RIGT}::test_a_free_port_passes_the_preflight",),
    ),
    Revert(
        "R1a: the default port back to the addon's own 9876",
        RIG,
        "_DEFAULT_PORT = 0\n",
        "_DEFAULT_PORT = 9876\n",
        (f"{RIGT}::test_the_default_port_is_an_unused_ephemeral_port",),
    ),
    Revert(
        "R1a control: _choose_port ignores an explicit --port",
        RIG,
        "    if requested:\n        return requested\n",
        "    if False:\n        return requested\n",
        (f"{RIGT}::test_an_explicit_port_is_honoured",),
    ),
    Revert(
        "C4: the port is not re-checked immediately before Popen",
        RIG,
        "    _require_port_free(port)\n    return subprocess.Popen(",
        "    return subprocess.Popen(",
        (f"{RIGT}::test_the_port_is_rechecked_immediately_before_blender_is_started",),
    ),
    Revert(
        "R1c: readiness back to a bare connect",
        RIG,
        "        if _receipt_matches(receipt, launch.nonce, launch.port) and _ping_answers(launch.port):",
        "        if _receipt_matches(receipt, launch.nonce, launch.port) and _bare_connect(launch.port):",
        (f"{RIGT}::test_readiness_requires_a_ping_round_trip_not_a_bare_connect",),
        also=BARE_CONNECT,
    ),
    Revert(
        "R1d: the receipt is not checked at all",
        RIG,
        '    try:\n        written = json.loads(receipt.read_text(encoding="utf-8"))\n'
        "    except (OSError, ValueError):\n        return False\n"
        '    return isinstance(written, dict) and written.get("nonce") == nonce and written.get("port") == port',
        "    return True",
        (f"{RIGT}::test_the_readiness_receipt_must_carry_the_rig_s_own_nonce_and_port",),
    ),
    Revert(
        "C4: the receipt's port is recorded but never checked",
        RIG,
        ' and written.get("port") == port',
        "",
        (f"{RIGT}::test_the_readiness_receipt_must_carry_the_rig_s_own_nonce_and_port",),
    ),
    Revert(
        "C4: a swallowed bind failure is left buried in the log tail",
        RIG,
        '    if _BIND_FAILURE_MARKER not in text:\n        return ""',
        '    if True:\n        return ""',
        (f"{RIGT}::test_a_startup_failure_names_a_bind_failure_the_addon_swallowed",),
    ),
    Revert(
        "A5: the log tail is formatted before the reader is joined",
        RIG,
        "            launch.drain.join(_SHUTDOWN_GRACE_SECONDS)\n            raise RigError(",
        "            raise RigError(",
        (f"{RIGT}::test_the_log_reader_is_joined_before_a_failure_quotes_its_log",),
    ),
    # --- what the rig may delete and overwrite ---
    Revert(
        "B3/B5: any --work-dir is claimed, marker or not",
        RIG,
        "        if existing:\n            raise RigError(_foreign_work_dir_message(work_dir, existing))",
        "        if False:\n            raise RigError(_foreign_work_dir_message(work_dir, existing))",
        (f"{RIGT}::test_a_populated_work_dir_the_rig_did_not_create_is_refused",),
    ),
    Revert(
        "B5: a real Blender resources root is no longer recognised as one",
        RIG,
        '_BLENDER_RESOURCE_ENTRIES = ("config", "datafiles", "extensions", "scripts", "userpref.blend")',
        "_BLENDER_RESOURCE_ENTRIES = ()",
        (f"{RIGT}::test_a_work_dir_that_is_a_real_blender_resources_root_is_refused",),
    ),
    Revert(
        "C1: an auto-executing startup/ tree is no longer recognised",
        RIG,
        '_AUTO_EXECUTED_SCRIPT_DIRS = ("startup", "modules")',
        "_AUTO_EXECUTED_SCRIPT_DIRS = ()",
        (f"{RIGT}::test_a_work_dir_holding_auto_executed_scripts_is_refused",),
    ),
    Revert(
        "B3 control: the work dir is never marked, so the rig refuses its own",
        RIG,
        '    marker.write_text(_OWNED_MARKER_TEXT, encoding="utf-8")\n\n\ndef _foreign_work_dir_message',
        "\n\ndef _foreign_work_dir_message",
        (f"{RIGT}::test_an_empty_or_rig_created_work_dir_is_claimed",),
    ),
    Revert(
        "R4: the marker no longer guards a directory the rig did not create",
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
        "C3/F10: <work-dir>/addons as a regular file is no longer refused",
        RIG,
        "    if directory.exists() and not directory.is_dir():\n"
        '        raise RigError(f"{directory} exists and is not a directory; the rig will not replace it.")\n',
        "",
        (f"{RIGT}::test_an_addons_path_that_is_a_regular_file_is_refused",),
    ),
    Revert(
        "R4 control: staging merges into its own previous stage instead of rebuilding it",
        RIG,
        "    if staged.exists():\n        shutil.rmtree(staged)\n",
        "",
        (f"{RIGT}::test_staging_replaces_its_own_previous_stage",),
    ),
    Revert(
        "B4: the whole addons/ tree is removed again, not just blender_mcp",
        RIG,
        "    if staged.exists():\n        shutil.rmtree(staged)\n",
        "    if addons.exists():\n        shutil.rmtree(addons)\n    addons.mkdir(parents=True)\n",
        (f"{RIGT}::test_staging_leaves_other_add_ons_in_its_own_directory_alone",),
    ),
    Revert(
        "R4: a symlinked addons/ directory is no longer refused",
        RIG,
        "    if directory.is_symlink():\n"
        '        raise RigError(f"{directory} is a symlink; refusing to write through it. '
        'Use a --work-dir the rig owns.")\n',
        "",
        (f"{RIGT}::test_staging_refuses_a_symlinked_addons_directory",),
    ),
    Revert(
        "R6: fixtures handed over by their original path",
        RIG,
        "        destination.unlink(missing_ok=True)\n        shutil.copy2(source, destination)\n"
        "        copies[name] = destination",
        "        copies[name] = source",
        (f"{RIGT}::test_fixtures_are_copied_so_a_scenario_cannot_write_through_to_the_original",),
    ),
    Revert(
        "B3: blends/ is created without the rig's marker, so a pre-placed file is overwritten",
        RIG,
        '    staged_dir = _rig_owned_subdirectory(work_dir, "blends")',
        '    staged_dir = work_dir / "blends"\n    staged_dir.mkdir(parents=True, exist_ok=True)',
        (f"{RIGT}::test_a_pre_placed_fixture_is_not_silently_overwritten",),
    ),
    Revert(
        "B3 control: every existing fixture destination is refused, reused work dir or not",
        RIG,
        "        destination.unlink(missing_ok=True)\n",
        '        if destination.exists():\n            raise RigError("refusing any existing destination")\n',
        (f"{RIGT}::test_the_rig_replaces_a_fixture_copy_it_made_itself",),
    ),
    Revert(
        "B3: copy2 follows a symlinked fixture destination again",
        RIG,
        "        if destination.is_symlink():\n"
        '            raise RigError(f"{destination} is a symlink; refusing to write through it.")\n',
        "",
        (f"{RIGT}::test_a_symlinked_fixture_destination_is_not_written_through",),
    ),
    Revert(
        "B3: two --blend fixtures may share one name again",
        RIG,
        "        if name in copies:\n"
        '            raise RigError(f"--blend {name} was given twice; the two copies would overwrite each other.")\n',
        "",
        (f"{RIGT}::test_two_fixtures_with_the_same_name_are_refused",),
    ),
    Revert(
        "R6: fixture names unvalidated, so one can escape the work dir",
        RIG,
        "    if not _FIXTURE_NAME.match(name):",
        "    if False:",
        (f"{RIGT}::test_a_fixture_name_cannot_escape_the_work_dir",),
    ),
    Revert(
        "F4: importing the scenario writes __pycache__ beside the caller's file again",
        RIG,
        "    sys.dont_write_bytecode = True\n    spec = importlib_util.spec_from_file_location",
        "    spec = importlib_util.spec_from_file_location",
        (f"{RIGT}::test_importing_a_scenario_leaves_no_pycache_beside_the_caller_s_file",),
    ),
    # --- liveness of the rig itself ---
    Revert(
        "R7: no deadline on the scenario",
        RIG,
        "    if thread.is_alive():\n        abandoned.set()",
        "    if False:\n        abandoned.set()",
        (f"{RIGT}::test_a_scenario_that_never_returns_is_abandoned_at_its_deadline",),
    ),
    Revert(
        "R7 control: the scenario's own failure is swallowed",
        RIG,
        "    if raised:\n        raise raised[0]",
        "    return",
        (f"{RIGT}::test_a_failing_scenario_still_reports_its_own_error",),
    ),
    Revert(
        "A3: an abandoned scenario may still send commands",
        RIG,
        "        self._refuse_if_abandoned(command_type)\n        self._sequence += 1",
        "        self._sequence += 1",
        (f"{RIGT}::test_an_abandoned_scenario_cannot_send_another_command",),
    ),
    Revert(
        "A3: the deadline no longer tells the rig it abandoned the scenario",
        RIG,
        "    if thread.is_alive():\n        abandoned.set()\n        raise RigError(",
        "    if thread.is_alive():\n        raise RigError(",
        (f"{RIGT}::test_the_deadline_silences_the_scenario_it_could_not_stop",),
    ),
    Revert(
        "A3: the transcript keeps growing after the verdict",
        RIG,
        "        if not self._abandoned.is_set():\n            print(line, flush=True)",
        "        print(line, flush=True)",
        (f"{RIGT}::test_the_deadline_silences_the_scenario_it_could_not_stop",),
    ),
    Revert(
        "A4: main catches Exception, so sys.exit(0) leaves the rig exiting 0",
        RIG,
        '    except BaseException as failure:\n        detail = ""',
        '    except Exception as failure:\n        detail = ""',
        (f"{RIGT}::test_a_scenario_that_exits_the_process_is_not_reported_as_a_pass",),
    ),
    Revert(
        "A2: Popen used as a context manager, reintroducing an unbounded wait()",
        RIG,
        "    blender = _launch_blender(work_dir, port, nonce, blender_scripts)\n"
        "    drain = _OutputDrain(blender, log_path, abandoned)\n"
        "    try:",
        "    with _launch_blender(work_dir, port, nonce, blender_scripts) as blender:\n"
        "        drain = _OutputDrain(blender, log_path, abandoned)\n"
        "        try:",
        (f"{RIGT}::test_the_process_is_never_waited_on_without_a_timeout",),
    ),
    Revert(
        "R2: Blender's output no longer drained to exhaustion",
        RIG,
        "            for line in stream:\n                log.write(line)",
        "            for line in [stream.readline()]:\n                log.write(line)",
        (f"{RIGT}::test_blender_s_output_is_drained_to_a_log_instead_of_filling_the_pipe",),
    ),
    Revert(
        "A1: the pipe decodes strictly again",
        RIG,
        '        errors="replace",\n',
        "",
        (f"{RIGT}::test_blender_s_output_is_decoded_leniently",),
    ),
    Revert(
        "A1: the reader dies on the first byte it cannot decode",
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
        "A3: the log reader keeps echoing after the verdict",
        RIG,
        "                if line.startswith(_ECHOED_PREFIXES) and not self._abandoned.is_set():",
        "                if line.startswith(_ECHOED_PREFIXES):",
        (f"{RIGT}::test_the_log_reader_stops_echoing_once_the_scenario_is_abandoned",),
    ),
    Revert(
        "A1: a reader that died is not reported, so Blender is blamed instead",
        RIG,
        "    if drain.failure is not None:\n        problems.append(\n"
        '            f"the rig\'s own log reader died with {type(drain.failure).__name__}: {drain.failure} "\n'
        '            "(the pipe was drained anyway, so this did not hang Blender, but the log may be short)"\n'
        "        )\n",
        "",
        (f"{RIGT}::test_teardown_reports_the_rig_s_own_log_reader_dying",),
    ),
    Revert(
        "A6: a reader still holding the pipe is not reported",
        RIG,
        "    if drain.is_alive():\n        problems.append(\n",
        "    if False:\n        problems.append(\n",
        (f"{RIGT}::test_teardown_reports_a_log_reader_that_outlived_blender",),
    ),
    Revert(
        "R2: failures no longer quote Blender's log",
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
        "D1: a packaging root outside src/ would distribute scripts/",
        PYPROJECT,
        'packages = [{ include = "blender_mcp", from = "src" }]',
        'packages = [{ include = "blender_mcp", from = "src" }, { include = "blender_rig", from = "scripts" }]',
        (f"{RIGT}::test_the_rig_is_not_importable_as_part_of_the_package",),
    ),
    # --- the container rig's static guards ---
    Revert(
        "docker: the build arg the entrypoint reads is no longer exported as ENV",
        DOCKERFILE,
        "ENV BLENDER_MAJOR_MINOR=${BLENDER_MAJOR_MINOR}\n",
        "",
        (f"{DOCKT}::test_build_args_the_entrypoint_reads_are_exported_as_env",),
    ),
    Revert(
        "docker: the entrypoint carries its own Blender version default again",
        ENTRYPOINT,
        'ADDONS_DIR="$HOME/.config/blender/${BLENDER_MAJOR_MINOR}/scripts/addons"',
        'ADDONS_DIR="$HOME/.config/blender/${BLENDER_MAJOR_MINOR:-5.2}/scripts/addons"',
        (f"{DOCKT}::test_entrypoint_does_not_hardcode_its_own_blender_version",),
    ),
    Revert(
        "docker: an unset Blender version no longer aborts the container",
        ENTRYPOINT,
        'BLENDER_MAJOR_MINOR="${BLENDER_MAJOR_MINOR:?must be set by the image (see Dockerfile ENV)}"',
        'BLENDER_MAJOR_MINOR="${BLENDER_MAJOR_MINOR}"',
        (f"{DOCKT}::test_entrypoint_fails_loudly_when_the_version_is_missing",),
    ),
    Revert(
        "docker: the Blender download URL hardcodes a version instead of interpolating it",
        DOCKERFILE,
        'RUN wget -q "https://download.blender.org/release/Blender${BLENDER_MAJOR_MINOR}/'
        'blender-${BLENDER_VERSION}-linux-x64.tar.xz" \\\n'
        "        -O /tmp/blender.tar.xz \\\n"
        "    && tar xf /tmp/blender.tar.xz -C /opt \\\n"
        "    && rm /tmp/blender.tar.xz \\\n"
        '    && ln -s "/opt/blender-${BLENDER_VERSION}-linux-x64/blender" /usr/local/bin/blender',
        'RUN wget -q "https://download.blender.org/release/Blender5.2/blender-5.2.1-linux-x64.tar.xz" \\\n'
        "        -O /tmp/blender.tar.xz \\\n"
        "    && tar xf /tmp/blender.tar.xz -C /opt \\\n"
        "    && rm /tmp/blender.tar.xz \\\n"
        '    && ln -s "/opt/blender-5.2.1-linux-x64/blender" /usr/local/bin/blender',
        (f"{DOCKT}::test_dockerfile_installs_the_blender_version_it_declares",),
    ),
    Revert(
        "docker: the advertised output root is not the path ./output is mounted at",
        COMPOSE,
        "      - ./output:/output",
        "      - ./renders:/output",
        (f"{DOCKT}::test_compose_declares_the_mounted_output_root",),
    ),
    Revert(
        "docker: the MCP port is published on every interface",
        COMPOSE,
        '      - "127.0.0.1:8000:8000"',
        '      - "8000:8000"',
        (f"{DOCKT}::test_compose_publishes_only_on_loopback",),
    ),
    Revert(
        "docker: Blender's own socket is published too",
        COMPOSE,
        '      - "127.0.0.1:8000:8000"',
        '      - "127.0.0.1:8000:8000"\n      - "127.0.0.1:9876:9876"',
        (f"{DOCKT}::test_compose_exposes_the_mcp_server_but_not_blender",),
    ),
    Revert(
        "docker: the MCP server listens on a port compose does not publish",
        ENTRYPOINT,
        "BLENDERMCP_HTTP_PORT=8000",
        "BLENDERMCP_HTTP_PORT=8001",
        (f"{DOCKT}::test_entrypoint_serves_the_mcp_server_over_http_on_the_published_port",),
    ),
    Revert(
        "entrypoint: the container asks for a transport compose's published port never serves",
        ENTRYPOINT,
        "BLENDERMCP_TRANSPORT=http",
        "BLENDERMCP_TRANSPORT=stdio",
        (f"{DOCKT}::test_entrypoint_serves_the_mcp_server_over_http_on_the_published_port",),
    ),
    Revert(
        "entrypoint: the readiness gate is gone, so the MCP server starts before Blender",
        ENTRYPOINT,
        "if ! wait_for_blender; then\n    terminate_children\n    exit 1\nfi\n\n",
        "",
        (f"{DOCKT}::test_entrypoint_waits_for_blender_before_starting_the_mcp_server",),
    ),
    Revert(
        "entrypoint: the readiness gate stops round-tripping Blender's socket",
        ENTRYPOINT,
        '"type": "ping"',
        '"type": "get_addon_info"',
        (f"{DOCKT}::test_entrypoint_waits_for_blender_before_starting_the_mcp_server",),
    ),
    Revert(
        "entrypoint: a bare wait is back, so Xvfb keeps a dead container alive",
        ENTRYPOINT,
        'for pid in "$blender_pid" "$mcp_pid" "$xvfb_pid"; do\n    wait "$pid" 2>/dev/null || true\ndone',
        "wait || true",
        (f"{DOCKT}::test_entrypoint_waits_only_on_the_processes_it_started",),
    ),
    Revert(
        "docker: Blender's socket bound to all interfaces",
        DOCKER_START,
        None,
        '\n# BlenderMCPServer(host="0.0.0.0") is exactly what must never ship.\n',
        (f"{DOCKT}::test_blender_keeps_its_socket_on_loopback",),
    ),
    Revert(
        "docker: dependencies resolved rather than installed from poetry.lock",
        DOCKERFILE,
        "poetry install --only main --no-root --no-interaction",
        "poetry install --no-root --no-interaction",
        (f"{DOCKT}::test_server_dependencies_are_installed_from_the_poetry_lock",),
    ),
    Revert(
        "docker: the build context excludes a file the Dockerfile COPYs",
        DOCKERIGNORE,
        "!docker/blender/healthcheck.py\n",
        "",
        (f"{DOCKT}::test_build_context_ignore_file_admits_everything_the_dockerfile_copies",),
    ),
    Revert(
        "docker: the healthcheck stops round-tripping Blender's socket",
        HEALTHCHECK,
        '"type": "ping"',
        '"type": "get_addon_info"',
        (f"{DOCKT}::test_compose_healthcheck_round_trips_both_blender_and_the_mcp_server",),
    ),
    Revert(
        "R19: the compose toolsets key is mistyped",
        COMPOSE,
        "      BLENDER_MCP_TOOLSETS: shot",
        "      BLENDER_MCP_TOOLSET: shot",
        (f"{DOCKT}::test_compose_pins_a_toolset_selection_the_server_can_resolve",),
    ),
    Revert(
        "R19: the compose toolsets value does not resolve",
        COMPOSE,
        "      BLENDER_MCP_TOOLSETS: shot",
        "      BLENDER_MCP_TOOLSETS: shto",
        (f"{DOCKT}::test_compose_pins_a_toolset_selection_the_server_can_resolve",),
    ),
    # --- the ported output_roots module ---
    Revert(
        "output_roots: the environment variable is no longer split on os.pathsep",
        ADDON_OUTPUT_ROOTS,
        "raw.split(os.pathsep)",
        "[raw]",
        (f"{ROOTST}::test_configured_roots_splits_the_environment_variable",),
    ),
    Revert(
        "output_roots: an unset variable falls back to a hardcoded root",
        ADDON_OUTPUT_ROOTS,
        'get(OUTPUT_ROOTS_ENV_VAR, "")',
        'get(OUTPUT_ROOTS_ENV_VAR, "/default")',
        (f"{ROOTST}::test_configured_roots_is_empty_when_unset",),
    ),
    Revert(
        "output_roots: blank entries are no longer stripped out",
        ADDON_OUTPUT_ROOTS,
        "return [entry.strip() for entry in raw.split(os.pathsep) if entry.strip()]",
        "return [entry for entry in raw.split(os.pathsep) if entry]",
        (f"{ROOTST}::test_configured_roots_ignores_blank_entries",),
    ),
    Revert(
        "output_roots control: writable_roots keeps nothing at all",
        ADDON_OUTPUT_ROOTS,
        "        seen.add(path)\n        roots.append(path)",
        "        seen.add(path)",
        (f"{ROOTST}::test_writable_roots_keeps_existing_writable_directories",),
    ),
    Revert(
        "output_roots: a path that does not exist is kept",
        ADDON_OUTPUT_ROOTS,
        "        if not os.path.isdir(path) or not os.access(path, os.W_OK):",
        "        if os.path.isfile(path) or (os.path.isdir(path) and not os.access(path, os.W_OK)):",
        (f"{ROOTST}::test_writable_roots_drops_paths_that_do_not_exist",),
    ),
    Revert(
        "output_roots: a plain file is accepted as a root",
        ADDON_OUTPUT_ROOTS,
        "        if not os.path.isdir(path) or not os.access(path, os.W_OK):",
        "        if not os.path.exists(path) or not os.access(path, os.W_OK):",
        (f"{ROOTST}::test_writable_roots_drops_files",),
    ),
    Revert(
        "output_roots: a read-only directory is offered as writable",
        ADDON_OUTPUT_ROOTS,
        "        if not os.path.isdir(path) or not os.access(path, os.W_OK):",
        "        if not os.path.isdir(path):",
        (f"{ROOTST}::test_writable_roots_drops_read_only_directories",),
    ),
    Revert(
        "output_roots: duplicates are no longer collapsed",
        ADDON_OUTPUT_ROOTS,
        "        if path in seen:\n            continue\n",
        "",
        (f"{ROOTST}::test_writable_roots_dedupes_while_preserving_order",),
    ),
    Revert(
        "output_roots: roots are reported relative to the addon's cwd",
        ADDON_OUTPUT_ROOTS,
        "        path = os.path.abspath(os.path.expanduser(str(candidate)))",
        "        path = os.path.expanduser(str(candidate))",
        (f"{ROOTST}::test_writable_roots_reports_absolute_paths",),
    ),
    Revert(
        "output_roots: empty and None candidates reach the scan",
        ADDON_OUTPUT_ROOTS,
        "        if not candidate:\n            continue\n",
        "",
        (f"{ROOTST}::test_writable_roots_ignores_empty_candidates",),
    ),
    Revert(
        "R1 wiring: the handshake ignores the deployment-configured roots",
        ADDON_SERVER_CORE,
        "            *configured_roots(),\n",
        "",
        (f"{ROOTST}::test_get_addon_info_reports_writable_output_roots",),
    ),
    Revert(
        "R1 wiring: an unconfigured Blender reports no writable root at all",
        ADDON_SERVER_CORE,
        '            getattr(bpy.app, "tempdir", None),\n            tempfile.gettempdir(),\n'
        '            os.path.expanduser("~"),\n',
        "",
        (f"{ROOTST}::test_get_addon_info_reports_roots_without_any_configuration",),
    ),
    # --- the handshake, and the MCP tool payload (defect ruling C's sixth site) ---
    Revert(
        "R17: the writable_output_roots parse removed from the handshake",
        ADDON_MANAGER,
        '            writable_output_roots=list(info.get("writable_output_roots") or []),\n',
        "",
        (
            f"{AMT}::test_handshake_surfaces_writable_output_roots",
            f"{AMT}::test_handshake_defaults_writable_output_roots_when_the_addon_omits_them",
        ),
    ),
    Revert(
        "R17: the handshake reorders the roots it was sent",
        ADDON_MANAGER,
        '            writable_output_roots=list(info.get("writable_output_roots") or []),',
        '            writable_output_roots=list(reversed(list(info.get("writable_output_roots") or []))),',
        (f"{AMT}::test_handshake_surfaces_writable_output_roots",),
    ),
    Revert(
        "F1: get_addon_status hardcodes an empty roots list",
        SERVER_CORE_TOOL,
        '            "writable_output_roots": result.writable_output_roots,',
        '            "writable_output_roots": [],',
        (
            f"{CORET}::test_get_addon_status_reports_the_writable_output_roots",
            f"{CORET}::test_get_addon_status_reports_no_roots_for_an_addon_that_does_not_send_them",
        ),
    ),
    Revert(
        "F1: get_addon_status reorders the roots the handshake gave it",
        SERVER_CORE_TOOL,
        '            "writable_output_roots": result.writable_output_roots,',
        '            "writable_output_roots": list(reversed(result.writable_output_roots)),',
        (f"{CORET}::test_get_addon_status_reports_the_writable_output_roots",),
    ),
    Revert(
        "F1: get_addon_status invents roots for an addon that sent none",
        SERVER_CORE_TOOL,
        '            "writable_output_roots": result.writable_output_roots,',
        '            "writable_output_roots": result.writable_output_roots or ["/invented"],',
        (f"{CORET}::test_get_addon_status_reports_no_roots_for_an_addon_that_does_not_send_them",),
    ),
    Revert(
        "defect ruling C: a payload key goes undocumented",
        SERVER_CORE_TOOL,
        '"capabilities" (feature flags reported by the addon), "blender_version", "writable_output_roots"',
        '"capabilities" (feature flags reported by the addon), "blender_version", writable output roots',
        (f"{CORET}::test_get_addon_status_documents_every_key_it_returns",),
    ),
    # --- the ported HTTP transport (ee25ffc) ---
    Revert(
        "transport: HTTP becomes the default, so every existing stdio client config breaks",
        SERVER_CLI,
        'env.get(TRANSPORT_ENV, "stdio")',
        'env.get(TRANSPORT_ENV, "http")',
        (
            f"{CLIT}::test_stdio_is_the_default",
            f"{CLIT}::test_http_settings_are_ignored_under_stdio",
            f"{CLIT}::test_main_serves_stdio_by_default",
        ),
    ),
    Revert(
        "transport: opting into HTTP alone binds every interface",
        SERVER_CLI,
        'DEFAULT_HTTP_HOST = "127.0.0.1"',
        'DEFAULT_HTTP_HOST = "0.0.0.0"',
        (f"{CLIT}::test_http_defaults_to_loopback_on_port_8000",),
    ),
    Revert(
        "transport: the default HTTP port is not the one the rig publishes",
        SERVER_CLI,
        "DEFAULT_HTTP_PORT = 8000",
        "DEFAULT_HTTP_PORT = 9000",
        (f"{CLIT}::test_http_defaults_to_loopback_on_port_8000",),
    ),
    Revert(
        "transport: the configured host and port are ignored",
        SERVER_CLI,
        "            host=env.get(HTTP_HOST_ENV, DEFAULT_HTTP_HOST),\n"
        "            port=_parse_port(env.get(HTTP_PORT_ENV, str(DEFAULT_HTTP_PORT))),",
        "            host=DEFAULT_HTTP_HOST,\n            port=DEFAULT_HTTP_PORT,",
        (f"{CLIT}::test_http_host_and_port_are_configurable",),
    ),
    Revert(
        "transport: the transport name is read raw, so compose's padding or casing is rejected",
        SERVER_CLI,
        'env.get(TRANSPORT_ENV, "stdio").strip().lower()',
        'env.get(TRANSPORT_ENV, "stdio")',
        (f"{CLIT}::test_transport_name_ignores_case_and_whitespace",),
    ),
    Revert(
        "transport: a misspelt transport silently serves stdio nobody reads",
        SERVER_CLI,
        "    raise ValueError(f\"{TRANSPORT_ENV} must be 'stdio' or 'http', got {name!r}\")",
        '    return TransportConfig(transport="stdio")',
        (
            f"{CLIT}::test_unknown_transport_is_rejected",
            f"{CLIT}::test_main_reports_a_bad_transport_without_serving",
        ),
    ),
    Revert(
        "transport: the HTTP port is no longer range-checked before anything binds",
        SERVER_CLI,
        "    if not 1 <= port <= MAX_PORT:",
        "    if False:",
        (
            f"{CLIT}::test_invalid_http_port_is_rejected[abc]",
            f"{CLIT}::test_invalid_http_port_is_rejected[0]",
            f"{CLIT}::test_invalid_http_port_is_rejected[65536]",
            f"{CLIT}::test_invalid_http_port_is_rejected[-1]",
        ),
    ),
    Revert(
        "transport: main() never dispatches to HTTP, so the container serves stdio into EOF",
        SERVER_CLI,
        '    if config.transport == "streamable-http":',
        "    if False:",
        (f"{CLIT}::test_main_serves_http_on_the_configured_address",),
    ),
    Revert(
        "transport: the configured bind address is never applied before serving",
        SERVER_CLI,
        "    mcp.settings.host = host\n    mcp.settings.port = port\n",
        "",
        (f"{CLIT}::test_main_serves_http_on_the_configured_address",),
    ),
    Revert(
        "transport: binding 0.0.0.0 drops the DNS-rebinding protection FastMCP built in",
        SERVER_CLI,
        "    mcp.settings.host = host\n    mcp.settings.port = port\n",
        "    mcp.settings.host = host\n    mcp.settings.port = port\n    mcp.settings.transport_security = None\n",
        (f"{CLIT}::test_binding_all_interfaces_keeps_dns_rebinding_protection",),
    ),
]


def collected_nodes() -> list[str]:
    """
    Ask pytest which nodes the task's new test files actually collect.

    Returns:
        list[str]: Every collected node id, plus the two nodes Task 1 added to
            an existing file.

    Raises:
        SystemExit: If collection failed, which would make coverage meaningless.

    """
    result = subprocess.run(
        [".venv/bin/python", "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider", *NEW_TEST_FILES],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"collection failed:\n{result.stdout}\n{result.stderr}")
    nodes = [line.strip() for line in result.stdout.splitlines() if "::test_" in line]
    return nodes + list(NEW_NODES_IN_EXISTING_FILES)


def coverage_gaps() -> list[str]:
    """
    List nodes that neither a revert nor a written-down exception accounts for.

    Returns:
        list[str]: The uncovered node ids, sorted.

    """
    covered = {node for revert in REVERTS for node in revert.nodes}
    return sorted(set(collected_nodes()) - covered - set(NOT_INDIVIDUALLY_FALSIFIABLE))


def apply(revert: Revert) -> str | None:
    """
    Write the reverted file and return what was there before.

    Args:
        revert: The revert to apply.

    Returns:
        str | None: The original contents, or None if the file did not exist.

    Raises:
        SystemExit: If the anchor text is not in the file, which would make the
            row silently test nothing.

    """
    original = revert.path.read_text(encoding="utf-8") if revert.path.exists() else None
    if revert.old is None:
        revert.path.write_text((original or "") + revert.new, encoding="utf-8")
        return original
    if original is None or revert.old not in original:
        raise SystemExit(f"anchor not found for {revert.label!r} in {revert.path}")
    revert.path.write_text(original.replace(revert.old, revert.new, 1) + revert.also, encoding="utf-8")
    return original


def restore(revert: Revert, original: str | None) -> None:
    """
    Put the file back exactly as it was.

    Args:
        revert: The revert that was applied.
        original: What `apply` returned.

    """
    if original is None:
        revert.path.unlink(missing_ok=True)
    else:
        revert.path.write_text(original, encoding="utf-8")


def run_nodes(nodes: tuple[str, ...]) -> tuple[bool, str]:
    """
    Run exactly the named nodes and report whether they failed.

    Args:
        nodes: Node ids to run.

    Returns:
        tuple[bool, str]: Whether pytest failed, and its summary line.

    """
    result = subprocess.run(
        [".venv/bin/python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", *nodes],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    summary = [line for line in result.stdout.splitlines() if " passed" in line or " failed" in line]
    tail = summary[-1] if summary else (result.stdout.strip().splitlines() or ["no output"])[-1]
    return result.returncode != 0, tail


@dataclass
class Report:
    """
    What one run of the matrix found.

    Attributes:
        survivors: Labels of reverts no named node noticed.
        gaps: Node ids nothing accounts for.

    """

    survivors: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


def main(argv: list[str] | None = None) -> int:
    """
    Run the matrix, or just print its coverage map.

    Args:
        argv: Arguments to parse; None means `sys.argv`.

    Returns:
        int: 0 when every revert broke its own nodes and nothing is uncovered.

    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="print the coverage map and exit")
    parser.add_argument("--only", default="", help="run only reverts whose label contains this substring")
    arguments = parser.parse_args(argv)

    report = Report(gaps=coverage_gaps())
    if arguments.list:
        for revert in REVERTS:
            print(f"{revert.label}")
            for node in revert.nodes:
                print(f"    node: {node}")
        for node, reason in sorted(NOT_INDIVIDUALLY_FALSIFIABLE.items()):
            print(f"[not individually falsifiable] {node}\n    reason: {reason}")
        print(f"\nreverts: {len(REVERTS)}; uncovered nodes: {len(report.gaps)}")
        for node in report.gaps:
            print(f"    UNCOVERED {node}")
        return 1 if report.gaps else 0

    for revert in REVERTS:
        if arguments.only and arguments.only not in revert.label:
            continue
        original = apply(revert)
        try:
            broke, tail = run_nodes(revert.nodes)
        finally:
            restore(revert, original)
        if not broke:
            report.survivors.append(revert.label)
        print(f"[{'FAILS as required' if broke else 'SURVIVED THE REVERT'}] {revert.label}")
        for node in revert.nodes:
            print(f"    node: {node}")
        print(f"    pytest: {tail}")

    print(f"\nreverts run: {len(REVERTS) if not arguments.only else 'subset'}")
    print(f"reverts that failed to break their own nodes: {len(report.survivors)}")
    for label in report.survivors:
        print(f"    SURVIVOR {label}")
    print(f"new test nodes with no revert and no written-down reason: {len(report.gaps)}")
    for node in report.gaps:
        print(f"    UNCOVERED {node}")
    return 1 if report.survivors or report.gaps else 0


if __name__ == "__main__":
    sys.exit(main())
