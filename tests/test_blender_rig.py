"""
Tests for the local GUI-Blender rig, `scripts/blender_rig.py`.

A rig defect does not fail loudly: it produces a plausible transcript of the wrong
thing. These tests cover which Blender the rig talks to, what it may delete or
overwrite, and whether it can hang. None of them launches Blender.
"""

import argparse
import ast
import contextlib
import json
import re
import socket
import subprocess
import sys
import threading
import time
import tomllib

from importlib import util as importlib_util
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RIG_PATH = REPO_ROOT / "scripts" / "blender_rig.py"
SRC_DIR = REPO_ROOT / "src"
ADDON_SERVER_CORE = SRC_DIR / "blender_mcp" / "bundled" / "addon" / "server_core.py"
RIG_SOURCE = RIG_PATH.read_text(encoding="utf-8")

# A child that writes one byte no UTF-8 decoder will accept, then enough output
# to overrun the ~64 KiB pipe buffer if anything stops reading at that byte.
UNDECODABLE_THEN_NOISY = (
    "import sys\n"
    "sys.stdout.buffer.write(b'\\xff not utf-8\\n')\n"
    "sys.stdout.buffer.flush()\n"
    "for index in range(20000):\n"
    "    print('noise', index)\n"
)


def _load_rig() -> ModuleType:
    """
    Import the rig from its path, since `scripts/` is not a package.

    Returns:
        ModuleType: The imported `blender_rig` module.

    """
    spec = importlib_util.spec_from_file_location("blender_rig_under_test", RIG_PATH)
    assert spec is not None and spec.loader is not None, f"{RIG_PATH} is not importable"
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rig = _load_rig()


def _addon_default_port() -> int:
    """
    Read the addon's default port from its source, which imports `bpy` and so cannot be imported here.

    A retyped copy of the number would not notice it change.

    Returns:
        int: `BlenderMCPServer.__init__`'s default `port`.

    Raises:
        AssertionError: If the addon no longer declares one, which would make every
            assertion against it vacuous.

    """
    tree = ast.parse(ADDON_SERVER_CORE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "BlenderMCPServer":
            continue
        for member in node.body:
            if not isinstance(member, ast.FunctionDef) or member.name != "__init__":
                continue
            defaulted = member.args.args[len(member.args.args) - len(member.args.defaults) :]
            for argument, default in zip(defaulted, member.args.defaults, strict=True):
                if argument.arg == "port":
                    return int(ast.literal_eval(default))
    raise AssertionError("BlenderMCPServer.__init__ no longer takes a defaulted port")


ADDON_DEFAULT_PORT = _addon_default_port()


def _function_source(name: str) -> str:
    """
    Slice one function's source out of the rig, so a guard cannot match elsewhere.

    Args:
        name: The function to extract.

    Returns:
        str: That function's source text.

    Raises:
        AssertionError: If the rig no longer defines that function, which would make
            every guard against it vacuous.

    """
    tree = ast.parse(RIG_SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(RIG_SOURCE, node) or ""
    raise AssertionError(f"scripts/blender_rig.py defines no {name}()")


def _listener() -> socket.socket:
    """
    Open a loopback listener on an ephemeral port, for occupied-port tests.

    Returns:
        socket.socket: The bound, listening socket; the caller closes it.

    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock


def _noisy_child(program: str, *, strict_decoding: bool = False) -> subprocess.Popen[str]:
    """
    Start a Python child whose combined output arrives on a pipe.

    Args:
        program: The `-c` program to run.
        strict_decoding: When True, decode the pipe strictly, as Python does by
            default, so one undecodable byte kills a reader.

    Returns:
        subprocess.Popen[str]: The running child.

    """
    return subprocess.Popen(
        [sys.executable, "-c", program],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors=None if strict_decoding else "replace",
    )


# --- which Blender, and which configuration, the rig launches ---


def test_blender_is_launched_with_factory_startup() -> None:
    """`--factory-startup` keeps the user's `userpref.blend` out of the run; no environment variable does."""
    assert "--factory-startup" in _function_source("_launch_blender"), (
        "the rig must launch Blender with --factory-startup, or it runs against the user's own preferences"
    )


def test_both_blender_user_resource_roots_point_at_the_work_dir(tmp_path: Path) -> None:
    """
    `BLENDER_USER_SCRIPTS` alone redirects only scripts.

    Config, datafiles and extensions would stay in the user's profile, so saving a
    `.blend` would rewrite their `recent-files.txt`.
    """
    environment = rig._child_environment(tmp_path)
    assert environment["BLENDER_USER_RESOURCES"] == str(tmp_path)
    assert environment["BLENDER_USER_SCRIPTS"] == str(tmp_path)


def test_the_launched_blender_is_pointed_at_the_work_dir_first(tmp_path: Path) -> None:
    """
    `BLENDERMCP_OUTPUT_ROOTS` puts the work dir first in the advertised roots.

    Without it the first offered root would be a temp directory Blender deletes on exit.
    """
    assert rig._child_environment(tmp_path)["BLENDERMCP_OUTPUT_ROOTS"] == str(tmp_path)


def test_blender_s_session_temp_dir_is_redirected_under_the_work_dir(tmp_path: Path) -> None:
    """`TMPDIR` moves Blender's session temp dir, with its autosaves and `quit.blend`, under `--work-dir`."""
    assert rig._child_environment(tmp_path)["TMPDIR"] == str(tmp_path / "tmp")


def test_an_inherited_pythonpath_cannot_shadow_the_staged_addon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An inherited `PYTHONPATH` or `BLENDERMCP_*` value does not reach Blender.

    `PYTHONPATH=$PWD/src` would make the bootstrap import the server package instead
    of the staged addon, which the rig would see only as a timeout.
    """
    monkeypatch.setenv("PYTHONPATH", str(SRC_DIR))
    monkeypatch.setenv("BLENDERMCP_OUTPUT_ROOTS", "/somewhere/else")
    environment = rig._child_environment(tmp_path)
    assert "PYTHONPATH" not in environment
    assert environment["BLENDERMCP_OUTPUT_ROOTS"] == str(tmp_path)


def test_blender_system_and_python_home_variables_cannot_redirect_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Pruning by prefix drops `BLENDER_SYSTEM_*`, and `PYTHONHOME` and `PYTHONSTARTUP` go too.

    Each can make Blender or the bootstrap load code the rig never staged.
    """
    redirected = ("BLENDER_SYSTEM_SCRIPTS", "BLENDER_SYSTEM_DATAFILES", "PYTHONHOME", "PYTHONSTARTUP")
    for name in redirected:
        monkeypatch.setenv(name, "/somewhere/else")

    environment = rig._child_environment(tmp_path)

    leaked = sorted(name for name in redirected if name in environment)
    assert not leaked, f"inherited variables that can redirect Blender or its Python reached the child: {leaked}"


# --- which process answers the port ---


def test_the_rig_refuses_a_port_something_is_already_listening_on() -> None:
    """
    The rig refuses a port that already has a listener, such as a developer's own Blender.

    Otherwise it would drive that session and report its answers as the rig's.
    """
    with _listener() as occupied:
        port = occupied.getsockname()[1]
        with pytest.raises(rig.RigError) as failure:
            rig._require_port_free(port)
    assert str(port) in str(failure.value), "the error must name the port so the caller can find the process"


def test_a_free_port_passes_the_preflight() -> None:
    """A check that always refuses would look the same as one that works."""
    rig._require_port_free(rig._choose_port(0))


def test_the_default_port_is_an_unused_ephemeral_port() -> None:
    """
    `--port` defaults to a free ephemeral port, not the addon's own.

    Checked through the parser, because `_choose_port(0)` passes whatever the default becomes.
    """
    parsed = rig._parse_arguments(["--work-dir", "/nonexistent", "--scenario", "/nonexistent/scenario.py"])
    assert parsed.port != ADDON_DEFAULT_PORT, "--port must not default to the addon's own port"
    port = rig._choose_port(parsed.port)
    assert port != ADDON_DEFAULT_PORT
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def test_an_explicit_port_is_honoured() -> None:
    """`--port` still has to win, or the preflight's advice is unfollowable."""
    with _listener() as occupied:
        requested = occupied.getsockname()[1]
    assert rig._choose_port(requested) == requested


def test_the_port_is_rechecked_immediately_before_blender_is_started() -> None:
    """
    The port is checked again just before `Popen`.

    Staging runs between the first check and Blender's bind, leaving time for something
    to take the port.
    """
    launch = _function_source("_launch_blender")
    assert "_require_port_free(port)" in launch, "the launch must re-check the port it is about to hand Blender"
    assert launch.index("_require_port_free(port)") < launch.index("subprocess.Popen("), (
        "the re-check has to happen before the process is started, not after"
    )


def test_readiness_requires_a_ping_round_trip_not_a_bare_connect() -> None:
    """
    Readiness needs a ping round trip, not a bare connect.

    The port accepts before the drain timer is registered, and `--blender-script`s run in
    that window.
    """
    readiness = _function_source("_wait_until_ready")
    assert "_ping_answers" in readiness, "readiness must go through the ping round-trip helper"
    ping = _function_source("_ping_answers")
    assert '"ping"' in ping, "the readiness probe must actually send a ping"
    assert "_read_frame" in ping, "the readiness probe must read a reply frame back"
    assert "recv" in _function_source("_read_frame"), "reading a frame means receiving bytes, not just connecting"


def test_the_readiness_receipt_must_carry_the_rig_s_own_nonce_and_port(tmp_path: Path) -> None:
    """
    Readiness needs a receipt carrying this run's nonce and port.

    A failed bind in Blender is only printed, so another process may be answering the port.
    """
    receipt = tmp_path / rig._RECEIPT_FILE_NAME
    assert rig._receipt_matches(receipt, "abc123", 49152) is False, "a missing receipt is not readiness"
    receipt.write_text("{not json", encoding="utf-8")
    assert rig._receipt_matches(receipt, "abc123", 49152) is False, "a half-written receipt is not readiness"
    receipt.write_text(json.dumps({"nonce": "someone-else", "pid": 1, "port": 49152}), encoding="utf-8")
    assert rig._receipt_matches(receipt, "abc123", 49152) is False, "another run's receipt is not readiness"
    receipt.write_text(json.dumps({"nonce": "abc123", "pid": 1, "port": 49153}), encoding="utf-8")
    assert rig._receipt_matches(receipt, "abc123", 49152) is False, "a receipt for another port is not readiness"
    receipt.write_text(json.dumps({"nonce": "abc123", "pid": 1, "port": 49152}), encoding="utf-8")
    assert rig._receipt_matches(receipt, "abc123", 49152) is True


def test_a_startup_failure_names_a_bind_failure_the_addon_swallowed(tmp_path: Path) -> None:
    """
    A readiness failure names a bind failure the addon only printed.

    Otherwise it reads as a ping that never came back, with the cause buried in the log tail.
    """
    log_path = tmp_path / "blender.log"
    child = _noisy_child("print('Failed to start server: [Errno 48] Address already in use')")
    drain = rig._OutputDrain(child, log_path, threading.Event())
    drain.start()
    launch = rig._Launch(child, 49152, tmp_path, "nonce", log_path, drain)

    with pytest.raises(rig.RigError) as failure:
        rig._wait_until_ready(launch, 5.0)

    message = str(failure.value)
    assert "socket bind failed" in message, "a swallowed bind failure must be named, not left in the tail"
    assert "Errno 48" in message, "the log tail carrying the real cause must still be quoted"


def test_the_log_reader_is_joined_before_a_failure_quotes_its_log() -> None:
    """Once Blender exits, its reader is joined before the log tail is quoted, so the tail has the last lines."""
    readiness = _function_source("_wait_until_ready")
    assert "drain.join" in readiness, "the reader must be joined once Blender has exited"
    assert readiness.index("drain.join") < readiness.index("_log_tail"), (
        "the tail is formatted before the reader is joined, so the last lines may not be in the file yet"
    )


# --- what the rig is allowed to delete and overwrite ---


def test_a_populated_work_dir_the_rig_did_not_create_is_refused(tmp_path: Path) -> None:
    """A populated `--work-dir` without the rig's marker is someone's data, and is refused."""
    (tmp_path / "notes.txt").write_text("someone's own work\n", encoding="utf-8")

    with pytest.raises(rig.RigError):
        rig._claim_work_dir(tmp_path)

    assert (tmp_path / "notes.txt").is_file()


def test_a_work_dir_that_is_a_real_blender_resources_root_is_refused(tmp_path: Path) -> None:
    """
    A work dir shaped like a Blender resources root is refused.

    `~/Library/Application Support/Blender/5.2` has no `addons/` child, so checking for
    `addons/` alone would point `BLENDER_USER_RESOURCES` at the user's configuration.
    """
    for name in ("config", "datafiles", "extensions"):
        (tmp_path / name).mkdir()
    (tmp_path / "userpref.blend").write_bytes(b"BLENDER-user-preferences")

    with pytest.raises(rig.RigError) as failure:
        rig._claim_work_dir(tmp_path)

    assert "Blender resources root" in str(failure.value), "the refusal must name the hazard it recognised"
    assert (tmp_path / "userpref.blend").is_file()


def test_a_work_dir_holding_auto_executed_scripts_is_refused(tmp_path: Path) -> None:
    """A work dir with a `startup/` tree is refused, since Blender runs every script there at launch."""
    (tmp_path / "startup").mkdir()
    (tmp_path / "startup" / "someone_elses.py").write_text("# runs inside Blender\n", encoding="utf-8")

    with pytest.raises(rig.RigError) as failure:
        rig._claim_work_dir(tmp_path)

    assert "startup/" in str(failure.value), "the refusal must name the auto-execution hazard"


def test_an_empty_or_rig_created_work_dir_is_claimed(tmp_path: Path) -> None:
    """The rig claims an empty work dir, and re-adopts its own along with what Blender wrote there."""
    work_dir = tmp_path / "work"

    rig._claim_work_dir(work_dir)

    assert (work_dir / rig._OWNED_MARKER_NAME).is_file()
    (work_dir / "config").mkdir()
    rig._claim_work_dir(work_dir)
    assert (work_dir / "config").is_dir(), "the rig must re-adopt a work dir it created, leftovers and all"


def test_a_symlinked_work_dir_is_resolved_before_anything_is_claimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`main()` resolves a symlinked `--work-dir`, so the rig reports the path it actually writes to."""
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    handed: list[Path] = []
    monkeypatch.setattr(rig, "_execute", lambda _arguments, work_dir: handed.append(work_dir))

    assert rig.main(["--work-dir", str(link), "--scenario", str(tmp_path / "scenario.py")]) == 0

    assert handed == [target.resolve()], f"--work-dir reached the run unresolved: {handed}"


def test_a_symlinked_work_dir_is_claimed_through_to_the_directory_it_points_at(tmp_path: Path) -> None:
    """A symlink to a directory the rig may own is claimed, so scratch space on another volume works."""
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    rig._claim_work_dir(link)

    assert (target / rig._OWNED_MARKER_NAME).is_file(), "the claim did not reach the directory the link points at"


def test_foreign_data_behind_a_symlinked_work_dir_is_still_refused(tmp_path: Path) -> None:
    """Data behind a symlinked work dir is refused by the marker check, which reads through the link."""
    target = tmp_path / "real"
    target.mkdir()
    (target / "IRREPLACEABLE.blend").write_bytes(b"BLENDER-somebody-elses-work")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(rig.RigError) as failure:
        rig._claim_work_dir(link)

    assert f"carries no {rig._OWNED_MARKER_NAME}" in str(failure.value), (
        "the refusal must come from the marker check reading through the link, not from refusing links as such"
    )
    assert (target / "IRREPLACEABLE.blend").read_bytes() == b"BLENDER-somebody-elses-work"


def test_staging_refuses_to_delete_an_addons_directory_it_does_not_own(tmp_path: Path) -> None:
    """
    Staging will not delete an `addons/blender_mcp` the rig did not create.

    Aimed at a real `BLENDER_USER_SCRIPTS` directory, it would delete the user's installed
    addon and their edits.
    """
    victim = tmp_path / "addons" / "blender_mcp"
    victim.mkdir(parents=True)
    (victim / "precious.py").write_text("# the user's own edits\n", encoding="utf-8")

    with pytest.raises(rig.RigError):
        rig._stage_addon(tmp_path)

    assert (victim / "precious.py").is_file(), "the rig deleted a tree it did not create"


def test_an_addons_path_that_is_a_regular_file_is_refused(tmp_path: Path) -> None:
    """An `addons` path that is a regular file gets the rig's own refusal, not a raw `OSError`."""
    (tmp_path / "addons").write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(rig.RigError):
        rig._stage_addon(tmp_path)

    assert (tmp_path / "addons").read_text(encoding="utf-8") == "not a directory\n"


def test_staging_replaces_its_own_previous_stage(tmp_path: Path) -> None:
    """Refusing everything would make the rig unusable on a reused work dir."""
    staged = tmp_path / "addons" / "blender_mcp"
    rig._stage_addon(tmp_path)
    (staged / "stale.py").write_text("# left over from the last run\n", encoding="utf-8")
    rig._stage_addon(tmp_path)
    assert (staged / "__init__.py").is_file(), "the addon should be staged from the checkout"
    assert not (staged / "stale.py").exists(), "a re-stage must start from the checkout, not merge into it"
    assert (tmp_path / "addons" / rig._OWNED_MARKER_NAME).is_file()


def test_staging_leaves_other_add_ons_in_its_own_directory_alone(tmp_path: Path) -> None:
    """
    Restaging replaces only `blender_mcp`, keeping add-ons a user installed beside it.

    The marker shows the rig created `addons/`, not everything in it.
    """
    rig._stage_addon(tmp_path)
    neighbour = tmp_path / "addons" / "some_other_addon"
    neighbour.mkdir()
    (neighbour / "__init__.py").write_text("# installed through Blender's UI\n", encoding="utf-8")

    rig._stage_addon(tmp_path)

    assert (neighbour / "__init__.py").is_file(), "the re-stage deleted an addon the rig never installed"
    assert (tmp_path / "addons" / "blender_mcp" / "__init__.py").is_file()


def test_staging_refuses_a_symlinked_addons_directory(tmp_path: Path) -> None:
    """
    Staging refuses a symlinked `addons/` directory.

    The link target carries the rig's marker, so only the symlink check can refuse it.
    """
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / rig._OWNED_MARKER_NAME).write_text("a marker the rig would accept\n", encoding="utf-8")
    (real / "precious.py").write_text("# the user's own edits\n", encoding="utf-8")
    (tmp_path / "addons").symlink_to(real, target_is_directory=True)

    with pytest.raises(rig.RigError):
        rig._stage_addon(tmp_path)

    assert (real / "precious.py").is_file(), "the rig wrote through a symlink it did not create"


def test_fixtures_are_copied_so_a_scenario_cannot_write_through_to_the_original(tmp_path: Path) -> None:
    """Fixtures are copied, so a scenario's save cannot overwrite the caller's original."""
    source = tmp_path / "source.blend"
    source.write_bytes(b"BLENDER-original")
    work_dir = tmp_path / "work"

    blends = rig._stage_blends(work_dir, [("fixture", source)])

    assert work_dir in blends["fixture"].parents, "the fixture handed to the scenario must live under the work dir"
    assert blends["fixture"].read_bytes() == b"BLENDER-original"
    blends["fixture"].write_bytes(b"BLENDER-overwritten")
    assert source.read_bytes() == b"BLENDER-original", "writing the copy reached the caller's original"


def test_a_pre_placed_fixture_is_not_silently_overwritten(tmp_path: Path) -> None:
    """An unmarked `blends/` directory is refused, so a file already there is not overwritten."""
    work_dir = tmp_path / "work"
    rig._claim_work_dir(work_dir)
    (work_dir / "blends").mkdir()
    victim = work_dir / "blends" / "fixture.blend"
    victim.write_bytes(b"BLENDER-not-the-rigs")
    source = tmp_path / "source.blend"
    source.write_bytes(b"BLENDER-incoming")

    with pytest.raises(rig.RigError):
        rig._stage_blends(work_dir, [("fixture", source)])

    assert victim.read_bytes() == b"BLENDER-not-the-rigs"


def test_the_rig_replaces_a_fixture_copy_it_made_itself(tmp_path: Path) -> None:
    """Refusing every existing destination would break every reused work dir."""
    work_dir = tmp_path / "work"
    first = tmp_path / "first.blend"
    first.write_bytes(b"BLENDER-first")
    second = tmp_path / "second.blend"
    second.write_bytes(b"BLENDER-second")

    rig._stage_blends(work_dir, [("fixture", first)])
    blends = rig._stage_blends(work_dir, [("fixture", second)])

    assert blends["fixture"].read_bytes() == b"BLENDER-second"


def test_a_symlinked_fixture_destination_is_not_written_through(tmp_path: Path) -> None:
    """`shutil.copy2` writes through a destination symlink, so staging a fixture refuses one."""
    work_dir = tmp_path / "work"
    rig._claim_work_dir(work_dir)
    blends = rig._rig_owned_subdirectory(work_dir, "blends")
    outside = tmp_path / "precious.blend"
    outside.write_bytes(b"BLENDER-precious")
    (blends / "fixture.blend").symlink_to(outside)
    source = tmp_path / "source.blend"
    source.write_bytes(b"BLENDER-incoming")

    with pytest.raises(rig.RigError):
        rig._stage_blends(work_dir, [("fixture", source)])

    assert outside.read_bytes() == b"BLENDER-precious"


def test_two_fixtures_with_the_same_name_are_refused(tmp_path: Path) -> None:
    """Both resolve to one filename, so the second would silently overwrite the first."""
    work_dir = tmp_path / "work"
    first = tmp_path / "first.blend"
    first.write_bytes(b"BLENDER-first")
    second = tmp_path / "second.blend"
    second.write_bytes(b"BLENDER-second")

    with pytest.raises(rig.RigError):
        rig._stage_blends(work_dir, [("fixture", first), ("fixture", second)])


def test_a_fixture_name_cannot_escape_the_work_dir() -> None:
    """The name becomes a filename, so `../` in it would place the copy outside."""
    with pytest.raises(argparse.ArgumentTypeError):
        rig._parse_blend(f"../escape={__file__}")


def test_importing_a_scenario_leaves_no_pycache_beside_the_caller_s_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Importing a scenario writes no `__pycache__/` beside it, outside `--work-dir`."""
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    scenario = tmp_path / "scenario.py"
    scenario.write_text("def run(rig):\n    return None\n", encoding="utf-8")

    rig._load_scenario(scenario)

    assert not (tmp_path / "__pycache__").exists(), "importing the scenario wrote outside the work dir"


# --- liveness of the rig itself ---


def test_a_scenario_that_never_returns_is_abandoned_at_its_deadline() -> None:
    """A scenario that never returns is abandoned at its deadline, so the rig cannot hang with it."""
    blocked = threading.Event()
    with pytest.raises(rig.RigError) as failure:
        rig._run_scenario_with_deadline(lambda _rig: blocked.wait(), None, 0.25)
    blocked.set()
    assert "--scenario-timeout" in str(failure.value), (
        "the error must name the flag that raises the deadline, not just report a number"
    )


def test_a_failing_scenario_still_reports_its_own_error() -> None:
    """The deadline must not swallow or reshape the failure the scenario raised."""

    def _run(_rig: object) -> None:
        raise AssertionError("the scenario's own complaint")

    with pytest.raises(AssertionError, match="the scenario's own complaint"):
        rig._run_scenario_with_deadline(_run, None, 30.0)


def test_an_abandoned_scenario_cannot_send_another_command(tmp_path: Path) -> None:
    """
    An abandoned scenario cannot send another command.

    Its daemon thread keeps running while the rig tears Blender down.
    """
    abandoned = threading.Event()
    abandoned.set()
    under_test = rig.BlenderRig(tmp_path, {}, 1, 1.0, abandoned)

    with pytest.raises(rig.RigError) as failure:
        under_test.send("ping")

    assert "abandoned" in str(failure.value)


def test_a_command_that_never_came_back_is_reported_as_possibly_still_running(tmp_path: Path) -> None:
    """
    A timed-out command is reported as possibly still running, not as failed.

    Nothing cancels a queued command, so it can still change the scene after the rig gives up.
    """
    with _listener() as silent:
        port = silent.getsockname()[1]
        under_test = rig.BlenderRig(tmp_path, {}, port, 0.25)

        with pytest.raises(rig.RigError) as failure:
            under_test.send("delete_object", {"name": "Cube"})

    message = str(failure.value)
    assert "delete_object" in message, "the error must name the command whose outcome is unknown"
    assert str(port) in message, "the error must name the port, so the operator can find the Blender in question"
    assert "may still be executing" in message.lower(), "a timed-out command must not be reported as a failure"
    assert "--command-timeout" in message, "the error must name the flag that raises the budget"
    assert isinstance(failure.value.__cause__, TimeoutError), "the original expiry must be chained, not discarded"


def test_one_frame_is_bounded_as_a_whole_not_one_recv_at_a_time() -> None:
    """The deadline covers the whole frame, so a peer trickling bytes without a newline cannot outlast it."""
    dribbles, interval, budget = 20, 0.1, 0.3
    with _listener() as listener:
        port = listener.getsockname()[1]

        def _dribble_without_a_newline() -> None:
            peer, _address = listener.accept()
            with peer, contextlib.suppress(OSError):
                for _index in range(dribbles):
                    time.sleep(interval)
                    peer.sendall(b"x")

        writer = threading.Thread(target=_dribble_without_a_newline, name="dribbling-peer", daemon=True)
        writer.start()
        client = socket.create_connection(("127.0.0.1", port), timeout=dribbles * interval)
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutError):
                rig._read_frame(client, budget)
        finally:
            elapsed = time.monotonic() - started
            client.close()
            writer.join(dribbles * interval + 5.0)

    assert elapsed < dribbles * interval * 0.75, f"_read_frame waited {elapsed:.1f}s for a {budget}s frame budget"


def test_the_deadline_silences_the_scenario_it_could_not_stop(capsys: pytest.CaptureFixture[str]) -> None:
    """Nothing is printed after the deadline's verdict, since stdout is the transcript."""
    blocked = threading.Event()
    abandoned = threading.Event()
    under_test = rig.BlenderRig(Path("/nonexistent"), {}, 1, 1.0, abandoned)

    with pytest.raises(rig.RigError):
        rig._run_scenario_with_deadline(lambda _rig: blocked.wait(), under_test, 0.25, abandoned)
    blocked.set()

    assert abandoned.is_set(), "the deadline must tell the rig it has abandoned the scenario"
    capsys.readouterr()
    under_test._echo("--> {sent after the verdict}")
    assert not capsys.readouterr().out, "the transcript grew after the verdict had been printed"


def test_a_scenario_that_exits_the_process_is_not_reported_as_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A scenario calling `sys.exit(0)` exits 1 with `RIG FAILED`, since the exit code is the verdict."""

    def _exit_cleanly(_arguments: argparse.Namespace, _work_dir: Path) -> None:
        raise SystemExit(0)

    monkeypatch.setattr(rig, "_execute", _exit_cleanly)

    status = rig.main(["--work-dir", str(tmp_path), "--scenario", str(tmp_path / "scenario.py")])

    captured = capsys.readouterr()
    assert status == 1, "a scenario that exits the process must not produce a zero exit code"
    assert "RIG PASSED" not in captured.out, "no verdict was earned"
    assert "RIG FAILED" in captured.err and "SystemExit" in captured.err


def test_the_process_is_never_waited_on_without_a_timeout() -> None:
    """
    Teardown never waits on Blender without a timeout.

    `Popen.__exit__` waits with none, so a process that survived SIGKILL would hang the rig.
    """
    execute = _function_source("_execute")
    assert "with _launch_blender" not in execute and "with subprocess.Popen" not in execute, (
        "Popen as a context manager reintroduces an unbounded wait() in __exit__"
    )
    assert "finally:" in execute and "_shut_down(" in execute, "teardown must still run on every path"
    shutdown = _function_source("_shut_down")
    assert ".wait(" in shutdown, "the guard is vacuous if teardown no longer waits at all"
    assert not re.search(r"blender\.wait\(\s*\)", shutdown), "every wait() on the process needs a timeout"


def test_blender_s_output_is_drained_to_a_log_instead_of_filling_the_pipe(tmp_path: Path) -> None:
    """
    Blender's output is drained to the log.

    An unread pipe would block Blender in `write()` on the main thread that answers commands.
    """
    log_path = tmp_path / "blender.log"
    child = _noisy_child("print('RIG: hello')\nfor i in range(5000): print('noise', i)")
    try:
        drain = rig._OutputDrain(child, log_path, threading.Event())
        drain.start()
        drain.join(30)
        assert child.wait(timeout=30) == 0
    finally:
        if child.poll() is None:
            child.kill()

    written = log_path.read_text(encoding="utf-8")
    assert "RIG: hello" in written
    assert "noise 4999" in written, "the drain stopped early, so the pipe would still fill"


def test_blender_s_output_is_decoded_leniently() -> None:
    """The pipe decodes with `errors="replace"`, so non-UTF-8 output does not end the reader."""
    assert 'errors="replace"' in _function_source("_launch_blender"), (
        "the pipe must decode leniently, or one non-UTF-8 byte ends the reader"
    )


def test_the_log_reader_survives_output_it_cannot_decode(tmp_path: Path) -> None:
    """
    The log reader keeps draining after an undecodable byte, and records the failure.

    A dead reader lets the pipe fill and deadlock Blender, which would look like Blender hanging.
    """
    log_path = tmp_path / "blender.log"
    child = _noisy_child(UNDECODABLE_THEN_NOISY, strict_decoding=True)
    try:
        drain = rig._OutputDrain(child, log_path, threading.Event())
        drain.start()
        assert child.wait(timeout=30) == 0, "the child blocked writing into a pipe nobody was draining"
        drain.join(10)
        assert not drain.is_alive(), "the reader never reached the end of the pipe"
    finally:
        if child.poll() is None:
            child.kill()

    assert isinstance(drain.failure, UnicodeDecodeError), (
        "the reader's own failure must be recorded, so teardown can blame the rig rather than Blender"
    )


def test_the_log_reader_stops_echoing_once_the_scenario_is_abandoned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Once the scenario is abandoned, Blender's lines still reach the log but not the transcript."""
    log_path = tmp_path / "blender.log"
    abandoned = threading.Event()
    abandoned.set()
    child = _noisy_child("print('RIG: printed after the verdict')")
    try:
        drain = rig._OutputDrain(child, log_path, abandoned)
        drain.start()
        drain.join(30)
        assert child.wait(timeout=30) == 0
    finally:
        if child.poll() is None:
            child.kill()

    assert "RIG: printed after the verdict" in log_path.read_text(encoding="utf-8"), "the log must still get it"
    assert "after the verdict" not in capsys.readouterr().out, "the transcript must not grow after the verdict"


def test_teardown_reports_the_rig_s_own_log_reader_dying(tmp_path: Path) -> None:
    """Teardown blames the rig's own log reader when that reader died."""
    log_path = tmp_path / "blender.log"
    child = _noisy_child(UNDECODABLE_THEN_NOISY, strict_decoding=True)
    drain = rig._OutputDrain(child, log_path, threading.Event())
    drain.start()
    assert child.wait(timeout=30) == 0, "the child blocked writing into a pipe nobody was draining"

    with pytest.raises(rig.RigError) as failure:
        rig._shut_down(child, drain, log_path)

    assert "log reader died" in str(failure.value)
    assert "UnicodeDecodeError" in str(failure.value)


def test_teardown_reports_a_log_reader_that_outlived_blender(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Teardown reports a reader still running after Blender exits.

    A grandchild holding the pipe means EOF never comes, and closing the pipe under the
    reader would fail on its thread.
    """
    monkeypatch.setattr(rig, "_SHUTDOWN_GRACE_SECONDS", 0.5)
    log_path = tmp_path / "blender.log"
    child = _noisy_child(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])\n"
        "print('RIG: parent is done, the grandchild still holds the pipe')\n"
    )
    try:
        drain = rig._OutputDrain(child, log_path, threading.Event())
        drain.start()
        assert child.wait(timeout=30) == 0, "the child must have forked and exited before teardown looks at it"

        with pytest.raises(rig.RigError) as failure:
            rig._shut_down(child, drain, log_path)

        assert "log reader is still running" in str(failure.value)
    finally:
        if child.poll() is None:
            child.kill()


def test_a_launched_blender_is_stopped_even_if_its_reader_cannot_be_constructed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A launched Blender is stopped even if its log reader cannot be constructed.

    The reader is created inside the `try` that guarantees teardown, or a Blender with an
    undrained pipe would be left running to deadlock.
    """
    work_dir = tmp_path / "work"
    scenario = tmp_path / "scenario.py"
    scenario.write_text("def run(rig):\n    return None\n", encoding="utf-8")
    stand_in = _noisy_child("import time\ntime.sleep(60)\n")

    def _cannot_be_constructed(*_arguments: object) -> None:
        raise MemoryError("no room for a reader")

    monkeypatch.setattr(rig, "_require_local_blender", lambda: None)
    monkeypatch.setattr(rig, "_launch_blender", lambda *_arguments: stand_in)
    monkeypatch.setattr(rig, "_OutputDrain", _cannot_be_constructed)
    arguments = rig._parse_arguments(["--work-dir", str(work_dir), "--scenario", str(scenario)])

    try:
        with pytest.raises(MemoryError):
            rig._execute(arguments, work_dir)

        assert stand_in.wait(timeout=30) != 0, "the launched process was left running with an undrained pipe"
    finally:
        if stand_in.poll() is None:
            stand_in.kill()


def test_teardown_tolerates_a_reader_that_never_started(tmp_path: Path) -> None:
    """
    Teardown tolerates a reader that never started.

    Joining an unstarted thread raises `RuntimeError`, which would hide why the reader
    failed to start.
    """
    log_path = tmp_path / "blender.log"
    child = _noisy_child("print('RIG: nobody ever read this')")
    try:
        drain = rig._OutputDrain(child, log_path, threading.Event())
        assert child.wait(timeout=30) == 0

        rig._shut_down(child, drain, log_path)
    finally:
        if child.poll() is None:
            child.kill()

    assert drain.failure is None, "a reader that never ran cannot have failed"
    assert not drain.is_alive()


def test_failures_quote_the_tail_of_that_log(tmp_path: Path) -> None:
    """A rig error with no Blender output in it leaves the failure unreadable."""
    log_path = tmp_path / "blender.log"
    log_path.write_text("".join(f"line {index}\n" for index in range(200)), encoding="utf-8")
    tail = rig._log_tail(log_path)
    assert "line 199" in tail
    assert "line 0" not in tail, "the tail should be bounded, not the whole log"
    assert not rig._log_tail(tmp_path / "absent.log"), "a missing log must not itself raise"


# --- the boundary the rig has to stay outside of ---


def test_no_packaged_module_references_the_rig() -> None:
    """No packaged module references the rig, which runs arbitrary scenario code."""
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in SRC_DIR.rglob("*.py")
        if "blender_rig" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"packaged modules referencing the rig: {offenders}"


def test_the_rig_is_not_importable_as_part_of_the_package() -> None:
    """
    Neither a `scripts/__init__.py` nor a packaging root outside `src/` can ship the rig.

    Both are read from the files that decide packaging, not inferred from paths.
    """
    assert not (REPO_ROOT / "scripts" / "__init__.py").exists()

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    roots = {pyproject["tool"]["setuptools"]["package-dir"][""]}
    roots.update(package["from"] for package in pyproject["tool"]["poetry"]["packages"])

    assert roots == {"src"}, f"a packaging root outside src/ would distribute the rig: {sorted(roots)}"
