"""
Guards for the local GUI-Blender rig, `scripts/blender_rig.py`.

The rig is the artefact every live-Blender transcript in this phase flows
through, so a defect in it does not fail loudly - it produces a plausible
transcript of the wrong thing. Its container sibling is protected by the static
guards in `tests/test_docker_rig.py`; this module does the same job for the
local rig, and adds behavioural coverage for the parts that decide *which*
Blender the rig is talking to, *what* it is allowed to delete or overwrite, and
whether it can hang or mislead instead of reporting.

Nothing here launches Blender: these are the checks that must hold on a machine
with no Blender at all, which is where the drift they catch would otherwise go
unnoticed until a phase gate.
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
    Import the rig from its path, since `scripts/` is deliberately not a package.

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
    Read the addon's own default port out of its source rather than retyping it.

    This is the port a developer's live Blender is already listening on, and the
    one the rig must not default to. Not importable from here - `server_core`
    imports `bpy` - but a guard carrying its own copy of the number it exists to
    diverge from cannot notice the number changing, which is the same drift
    `tests/test_docker_rig.py` imports its environment variable names to avoid.

    Returns:
        int: `BlenderMCPServer.__init__`'s default `port`.

    Raises:
        AssertionError: If the addon no longer declares one, which would make
            every assertion written against it vacuous.

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
        AssertionError: If the rig no longer defines that function, which would
            make every guard written against it silently vacuous.

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
        strict_decoding: When True, decode the pipe the way Python does by
            default, which is what turns one undecodable byte into a dead reader.

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


# --- which Blender, and which configuration, the rig actually launches ---


def test_blender_is_launched_with_factory_startup() -> None:
    """
    `--factory-startup` is what keeps the user's `userpref.blend` out of the run.

    No environment variable does this: preferences are read from the user's
    profile unless Blender is told to ignore them entirely.
    """
    assert "--factory-startup" in _function_source("_launch_blender"), (
        "the rig must launch Blender with --factory-startup, or it runs against the user's own preferences"
    )


def test_both_blender_user_resource_roots_point_at_the_work_dir(tmp_path: Path) -> None:
    """
    `BLENDER_USER_SCRIPTS` alone redirects only scripts.

    Measured against Blender 5.2.1: config, datafiles and extensions still
    resolve under the user's own profile, so opening or saving a `.blend`
    rewrites their `recent-files.txt`. `BLENDER_USER_RESOURCES` is what moves
    the rest.
    """
    environment = rig._child_environment(tmp_path)
    assert environment["BLENDER_USER_RESOURCES"] == str(tmp_path)
    assert environment["BLENDER_USER_SCRIPTS"] == str(tmp_path)


def test_the_launched_blender_is_pointed_at_the_work_dir_first(tmp_path: Path) -> None:
    """
    `BLENDERMCP_OUTPUT_ROOTS` makes the work dir *lead* the advertised list.

    It does not make it the only entry, and the test is named for what it
    checks: `server_core._writable_output_roots()` prepends the configured roots
    and then appends `bpy.app.tempdir`, `tempfile.gettempdir()` and `~`, so the
    user's home directory is still advertised. Narrowing the addon's own default
    candidates is Task 5's decision. Unset, the work dir would not appear at all
    and the first offered root would be a temp directory Blender deletes on exit.
    """
    assert rig._child_environment(tmp_path)["BLENDERMCP_OUTPUT_ROOTS"] == str(tmp_path)


def test_blender_s_session_temp_dir_is_redirected_under_the_work_dir(tmp_path: Path) -> None:
    """
    `bpy.app.tempdir` follows `TMPDIR`, and nothing else the rig sets moves it.

    Autosaves, `quit.blend` and render previews land there, so leaving it at the
    user's `$TMPDIR` means the rig writes outside `--work-dir` on every run -
    which the rig's own transcript printed, while its docstring claimed
    otherwise.
    """
    assert rig._child_environment(tmp_path)["TMPDIR"] == str(tmp_path / "tmp")


def test_an_inherited_pythonpath_cannot_shadow_the_staged_addon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    `PYTHONPATH=$PWD/src` makes Blender's Python resolve `blender_mcp` to the server package.

    The bootstrap's `from blender_mcp.server_core import ...` then imports the
    wrong tree and fails inside Blender, where the rig would see only a timeout.
    Inherited `BLENDERMCP_*` values are dropped for the same reason: they would
    silently override the rig's own isolation.
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
    A denylist of `BLENDER_USER_*` let `BLENDER_SYSTEM_SCRIPTS` straight through.

    That variable redirects Blender's *system* scripts tree - the same shadowing
    hazard one prefix over from the one the rig already guarded. `PYTHONHOME`
    and `PYTHONSTARTUP` are the matching pair on the Python side: either can
    make the bootstrap's import resolve somewhere the rig never staged.
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
    The addon's own default port is where a developer's live Blender already sits.

    `SO_REUSEADDR` does not let a second process bind it on macOS, so a rig that
    treats "a connect succeeded" as readiness would drive the user's real
    session and report its answers as the rig's.
    """
    with _listener() as occupied:
        port = occupied.getsockname()[1]
        with pytest.raises(rig.RigError) as failure:
            rig._require_port_free(port)
    assert str(port) in str(failure.value), "the error must name the port so the caller can find the process"


def test_a_free_port_passes_the_preflight() -> None:
    """A check that always refuses would be indistinguishable from one that works."""
    rig._require_port_free(rig._choose_port(0))


def test_the_default_port_is_an_unused_ephemeral_port() -> None:
    """
    Defaulting to the addon's 9876 aims the rig at whatever is already there.

    Asking the kernel for a port instead means the common case - a developer
    with Blender open - is not a collision at all. Asserted through the parser,
    not by calling `_choose_port(0)` directly: the latter passes whatever
    `--port`'s default becomes, which is the value actually at issue.
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
    The pre-flight and Blender's own `bind()` are minutes apart on a cold start.

    Staging the addon and copying fixtures happen in between, so re-running the
    check just before `Popen` is what keeps that window down to this process's
    own scheduling rather than the whole of setup. It cannot close the window -
    only Blender's `bind()` is authoritative - which is why the receipt carries
    the port too.
    """
    launch = _function_source("_launch_blender")
    assert "_require_port_free(port)" in launch, "the launch must re-check the port it is about to hand Blender"
    assert launch.index("_require_port_free(port)") < launch.index("subprocess.Popen("), (
        "the re-check has to happen before the process is started, not after"
    )


def test_readiness_requires_a_ping_round_trip_not_a_bare_connect() -> None:
    """
    `start()` binds and listens before the drain timer is registered.

    So the port accepts while nothing dequeues, and `--blender-script` arguments
    run in that window. `docker/blender/healthcheck.py` states this same lesson
    and round-trips a ping; the local rig has to match its sibling.
    """
    readiness = _function_source("_wait_until_ready")
    assert "_ping_answers" in readiness, "readiness must go through the ping round-trip helper"
    ping = _function_source("_ping_answers")
    assert '"ping"' in ping, "the readiness probe must actually send a ping"
    assert "_read_frame" in ping, "the readiness probe must read a reply frame back"
    assert "recv" in _function_source("_read_frame"), "reading a frame means receiving bytes, not just connecting"


def test_the_readiness_receipt_must_carry_the_rig_s_own_nonce_and_port(tmp_path: Path) -> None:
    """
    A bind failure inside Blender is only a `print`; something else can answer the port.

    Requiring a receipt the rig's own launch wrote turns that into a rig error
    instead of a passing run against a stranger. The port is checked as well as
    the nonce: the bootstrap has always recorded it, and it is what catches a
    stale receipt this same run wrote for an earlier launch in a reused work dir.
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
    `BlenderMCPServer.start()` catches its own `OSError` and only prints it.

    So a port taken between the pre-flight and Blender's `bind()` used to read
    out as nothing more specific than "never answered a ping", with the real
    cause - `Failed to start server: [Errno 48]` - buried in the log tail.
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
    """
    The most diagnostic lines are the last ones, and they arrive last.

    Blender has reached EOF by the time `_wait_until_ready` notices it exited,
    so the reader will finish; quoting the tail before joining it races the
    thread for exactly the lines that explain the failure.
    """
    readiness = _function_source("_wait_until_ready")
    assert "drain.join" in readiness, "the reader must be joined once Blender has exited"
    assert readiness.index("drain.join") < readiness.index("_log_tail"), (
        "the tail is formatted before the reader is joined, so the last lines may not be in the file yet"
    )


# --- what the rig is allowed to delete and overwrite ---


def test_a_populated_work_dir_the_rig_did_not_create_is_refused(tmp_path: Path) -> None:
    """
    The marker is the whole of the rig's claim to `--work-dir`; without it, it has none.

    Everything else it writes is inside this directory, so this one check is
    what stands between a mistyped `--work-dir` and somebody's data.
    """
    (tmp_path / "notes.txt").write_text("someone's own work\n", encoding="utf-8")

    with pytest.raises(rig.RigError):
        rig._claim_work_dir(tmp_path)

    assert (tmp_path / "notes.txt").is_file()


def test_a_work_dir_that_is_a_real_blender_resources_root_is_refused(tmp_path: Path) -> None:
    """
    `~/Library/Application Support/Blender/5.2` has no `addons/` child.

    The real one is `scripts/addons`, so a guard that looked only at `addons/`
    waved that path through and then pointed `BLENDER_USER_RESOURCES` at the
    user's genuine configuration - the isolation defeated through an input
    rather than a missing environment variable.
    """
    for name in ("config", "datafiles", "extensions"):
        (tmp_path / name).mkdir()
    (tmp_path / "userpref.blend").write_bytes(b"BLENDER-user-preferences")

    with pytest.raises(rig.RigError) as failure:
        rig._claim_work_dir(tmp_path)

    assert "Blender resources root" in str(failure.value), "the refusal must name the hazard it recognised"
    assert (tmp_path / "userpref.blend").is_file()


def test_a_work_dir_holding_auto_executed_scripts_is_refused(tmp_path: Path) -> None:
    """
    `BLENDER_USER_SCRIPTS=<work-dir>` makes `<work-dir>/startup/` auto-execute.

    Blender imports every `.py` there at launch and puts `modules/` on
    `sys.path`, so a reused or mistargeted work dir injects code into the very
    Blender the rig is about to trust - a code-execution surface opened by the
    isolation mechanism itself, not by a caller-supplied `--python`.
    """
    (tmp_path / "startup").mkdir()
    (tmp_path / "startup" / "someone_elses.py").write_text("# runs inside Blender\n", encoding="utf-8")

    with pytest.raises(rig.RigError) as failure:
        rig._claim_work_dir(tmp_path)

    assert "startup/" in str(failure.value), "the refusal must name the auto-execution hazard"


def test_an_empty_or_rig_created_work_dir_is_claimed(tmp_path: Path) -> None:
    """
    Refusing everything would make the rig unusable, which is not a safety property.

    A second run has to succeed against the `config/` and `datafiles/` trees
    Blender itself wrote there during the first one.
    """
    work_dir = tmp_path / "work"

    rig._claim_work_dir(work_dir)

    assert (work_dir / rig._OWNED_MARKER_NAME).is_file()
    (work_dir / "config").mkdir()
    rig._claim_work_dir(work_dir)
    assert (work_dir / "config").is_dir(), "the rig must re-adopt a work dir it created, leftovers and all"


def test_a_symlinked_work_dir_is_resolved_before_anything_is_claimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Resolution at the boundary is what makes the marker check the only guard needed.

    `_claim_work_dir` used to carry an `is_symlink()` refusal that could never
    fire, because `main()` resolves `--work-dir` first. The refusal is gone; the
    resolution it was shadowing is load-bearing and is pinned here instead, since
    without it the rig would report, log and advertise a path that is not the one
    it is writing to.
    """
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    handed: list[Path] = []
    monkeypatch.setattr(rig, "_execute", lambda _arguments, work_dir: handed.append(work_dir))

    assert rig.main(["--work-dir", str(link), "--scenario", str(tmp_path / "scenario.py")]) == 0

    assert handed == [target.resolve()], f"--work-dir reached the run unresolved: {handed}"


def test_a_symlinked_work_dir_is_claimed_through_to_the_directory_it_points_at(tmp_path: Path) -> None:
    """
    A link to a directory the rig may own is claimed, not refused.

    Deliberate: an `is_symlink()` refusal here was unreachable in production and
    read as a safety control while protecting nothing, and refusing links
    outright would break the ordinary case of a scratch directory living on
    another volume. The marker lands in the real directory, which is where every
    subsequent write goes.
    """
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    rig._claim_work_dir(link)

    assert (target / rig._OWNED_MARKER_NAME).is_file(), "the claim did not reach the directory the link points at"


def test_foreign_data_behind_a_symlinked_work_dir_is_still_refused(tmp_path: Path) -> None:
    """
    The marker check reads through the link, so it protects the real directory.

    This is the whole reason a blanket symlink refusal was not needed: a link
    aimed at somebody's data is refused *because that data is there*, with the
    same message and the same reasoning as if the path had been given directly.
    """
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
    `--work-dir` is exactly the shape of a real `BLENDER_USER_SCRIPTS` directory.

    Pointed at one, an unguarded `rmtree` deletes the user's installed addon and
    any local edits. `addon_manager.install_addon` already sets the convention:
    remove only what you can positively identify as yours.
    """
    victim = tmp_path / "addons" / "blender_mcp"
    victim.mkdir(parents=True)
    (victim / "precious.py").write_text("# the user's own edits\n", encoding="utf-8")

    with pytest.raises(rig.RigError):
        rig._stage_addon(tmp_path)

    assert (victim / "precious.py").is_file(), "the rig deleted a tree it did not create"


def test_an_addons_path_that_is_a_regular_file_is_refused(tmp_path: Path) -> None:
    """
    A regular file hits neither the symlink branch nor the `is_dir()` branch.

    It is the one shape of `<work-dir>/addons` that reached `mkdir`/`rmtree`
    directly, so the caller got an uncaught `OSError` instead of the rig's own
    explanation of what it refused and why.
    """
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
    The marker proves the rig created the directory, not that it wrote everything in it.

    One run writes the marker; the user then installs real add-ons through
    Blender's UI into the same `BLENDER_USER_SCRIPTS` tree; a `rmtree` of the
    whole `addons/` directory on the next run deletes them all, blessed by a
    marker the rig wrote itself. Remove only `blender_mcp`.
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
    A symlink makes `exists()` and `rmtree` disagree about what is being deleted.

    The target deliberately carries the rig's own marker. Without it the marker
    check refuses the directory first and this test passes whether or not the
    symlink check exists at all - which is what it did, until the revert matrix
    was extended to every node and caught it.
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
    """
    A scenario that calls a save command is handed a path, not a policy.

    Handing over the caller's own `.blend` means the first save overwrites the
    source fixture in place, which no later task could detect.
    """
    source = tmp_path / "source.blend"
    source.write_bytes(b"BLENDER-original")
    work_dir = tmp_path / "work"

    blends = rig._stage_blends(work_dir, [("fixture", source)])

    assert work_dir in blends["fixture"].parents, "the fixture handed to the scenario must live under the work dir"
    assert blends["fixture"].read_bytes() == b"BLENDER-original"
    blends["fixture"].write_bytes(b"BLENDER-overwritten")
    assert source.read_bytes() == b"BLENDER-original", "writing the copy reached the caller's original"


def test_a_pre_placed_fixture_is_not_silently_overwritten(tmp_path: Path) -> None:
    """
    `<work-dir>/blends/<name>.blend` was `copy2`'d over whatever was already there.

    The rig's marker guarded `addons/` and nothing else, so the one directory it
    copies *user data* into had no provenance check at all - and the rig's
    stated rule is that it deletes nothing under `--work-dir` it did not create.
    """
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
    """
    `shutil.copy2` follows a destination symlink and writes through it.

    That is the same hazard `_stage_addon` was hardened against, left open in
    the function that copies the caller's own fixtures.
    """
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
    """Both resolve to one filename, so the second silently became the first."""
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
    """
    `exec_module` writes `__pycache__/` next to the *caller's* scenario, not ours.

    That is outside `--work-dir`, so it falsified the containment claim on every
    run - `scripts/__pycache__/` in this repo is the same effect from this very
    module. The container half already sets `PYTHONDONTWRITEBYTECODE=1`.
    """
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    scenario = tmp_path / "scenario.py"
    scenario.write_text("def run(rig):\n    return None\n", encoding="utf-8")

    rig._load_scenario(scenario)

    assert not (tmp_path / "__pycache__").exists(), "importing the scenario wrote outside the work dir"


# --- liveness of the rig itself ---


def test_a_scenario_that_never_returns_is_abandoned_at_its_deadline() -> None:
    """
    The rig's documented synchronization is "poll for a file", which waits forever.

    A harness whose purpose is to demonstrate that Blender did not hang cannot
    itself hang, or the evidence is a stuck process and no transcript.
    """
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
    The scenario thread is a daemon: it is abandoned at the deadline, never stopped.

    So it keeps running while the rig prints its verdict and tears Blender down,
    and `send()` had nothing to check. A command issued after `RIG FAILED` would
    arrive at a Blender that is already being killed.
    """
    abandoned = threading.Event()
    abandoned.set()
    under_test = rig.BlenderRig(tmp_path, {}, 1, 1.0, abandoned)

    with pytest.raises(rig.RigError) as failure:
        under_test.send("ping")

    assert "abandoned" in str(failure.value)


def test_a_command_that_never_came_back_is_reported_as_possibly_still_running(tmp_path: Path) -> None:
    """
    A timed-out command is an unknown outcome, not a failure, and must read as one.

    Measured against the real addon with a 0.5s command timeout: the rig raised
    `TimeoutError: timed out`, and the command it had given up on then ran on
    Blender's next main-thread tick and mutated the scene. Nothing cancels a
    queued command, and its reply is lost in silence - a `sendall` to a
    peer-closed TCP socket succeeds at the kernel level until the RST arrives, so
    the addon's own "client disconnected" branch is usually never reached. An
    operator told "failure" about a command that changed the scene draws exactly
    the wrong conclusion, so the error has to name the command, the port and the
    doubt, and it has to be a `RigError` as both docstrings promise.
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
    """
    A socket timeout bounds one `recv`, so a dribbling peer reads for ever.

    Measured: a peer sending one byte every 0.4s kept `_read_frame` reading for
    3.2s against a 1.0s socket timeout, because every individual `recv` came
    back inside the limit. A harness whose verdict is "Blender did not hang"
    must not own a wait it cannot bound, so the frame carries a deadline of its
    own and every `recv` gets only what is left of it.
    """
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
    """
    Stdout *is* the evidence artefact, so nothing may land in it after the verdict.

    An abandoned scenario that keeps printing leaves `-->`/`<--` pairs below
    `RIG FAILED`, or a `<--` with no matching `-->`, and a reader has no way to
    tell which half of the transcript to believe.
    """
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
    """
    `sys.exit(0)` in a scenario is a `SystemExit`, which is not an `Exception`.

    Catching only `Exception` let it out of `main` untouched, so the rig exited
    **0** having printed neither `RIG PASSED` nor `RIG FAILED`. The rig's exit
    code is its verdict, and every task in this phase is gated on it.
    """

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
    CPython's `Popen.__exit__` calls `self.wait()` with no timeout.

    So `with _launch_blender(...) as blender:` turns the one teardown failure the
    rig anticipates - a process that survived `SIGKILL` - into a permanent hang,
    because that exception propagates into `__exit__`, which then waits forever
    on exactly the process that just proved it will not die. Teardown is
    explicit, in `try`/`finally`, for that reason.
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
    `stdout=PIPE` with no reader deadlocks Blender in `write()` on its main thread.

    That is the thread the drain loop runs on, so the rig's own logging would be
    what stops commands being answered - and the log is also the only place a
    bootstrap failure is visible.
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
    """
    Blender's output is not guaranteed UTF-8: native chatter, a crash dump, a path.

    Under Python's default strict decoding one such byte raises inside the
    reader, and the fallback below - which exists for everything this does not
    cover - would then be doing the work on every ordinary run.
    """
    assert 'errors="replace"' in _function_source("_launch_blender"), (
        "the pipe must decode leniently, or one non-UTF-8 byte ends the reader"
    )


def test_the_log_reader_survives_output_it_cannot_decode(tmp_path: Path) -> None:
    """
    A dead reader silently restores the deadlock draining exists to prevent.

    `_drain_output`'s own docstring said it "must never stop early"; one
    undecodable byte ended the thread, the pipe then filled at ~64 KiB, and
    Blender blocked in `write()` on its main thread with no diagnostic at all -
    a failure that reads as "Blender hung".
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
    """
    Blender keeps printing while it is being torn down, long after the verdict.

    Those lines still belong in the log - that is the evidence - but not in the
    transcript below `RIG FAILED`.
    """
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
    """
    "Blender hung" is the wrong diagnosis when it was the rig's reader that died.

    The reader keeps the pipe drained either way, so nothing hangs - but the log
    is short and the run's failure has to name its real cause.
    """
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
    A Blender grandchild that inherited the pipe means no EOF ever arrives.

    The reader then sits in `readline` forever; closing the pipe under it raises
    `ValueError` on a daemon thread and leaks the log handle, which is why
    teardown reports it instead of proceeding as if the join had worked.
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
    A Blender with nobody on its pipe is the one process the rig must never orphan.

    `_launch_blender` hands back a process whose output is already accumulating
    in a pipe, so the statement that gives it a reader has to be inside the
    `try` that guarantees teardown. Constructed outside it, a failure there -
    "can't start new thread" is the realistic one - left a GUI Blender running
    with an undrained pipe, which deadlocks it in `write()` at ~64 KiB: the
    exact hazard the reader exists to prevent, caused by the reader's absence.
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
    `Thread.join` on a thread that was never started raises `RuntimeError`.

    It would be raised from teardown's own `finally`, after Blender had already
    been terminated and before any diagnosis was built - so the run's reported
    cause would be "cannot join thread before it is started" rather than the
    "can't start new thread" that actually happened.
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
    """A rig error with no Blender output in it is what made the last failure unreadable."""
    log_path = tmp_path / "blender.log"
    log_path.write_text("".join(f"line {index}\n" for index in range(200)), encoding="utf-8")
    tail = rig._log_tail(log_path)
    assert "line 199" in tail
    assert "line 0" not in tail, "the tail should be bounded, not the whole log"
    assert not rig._log_tail(tmp_path / "absent.log"), "a missing log must not itself raise"


# --- the boundary the rig has to stay outside of ---


def test_no_packaged_module_references_the_rig() -> None:
    """
    `scripts/` is not packaged and the rig executes an arbitrary scenario module.

    That is acceptable only while nothing shipped can reach it: an import from
    `src/` would put an arbitrary-code-execution path inside the distributed
    package. Keep it true by test rather than by luck.
    """
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in SRC_DIR.rglob("*.py")
        if "blender_rig" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"packaged modules referencing the rig: {offenders}"


def test_the_rig_is_not_importable_as_part_of_the_package() -> None:
    """
    A `scripts/__init__.py`, or a packaging root outside `src/`, would ship the rig.

    Both halves are checked against the files that decide it. An earlier version
    of this test also compared `os.path.commonpath([RIG_PATH, SRC_DIR])` to the
    repo root, which cannot fail: both constants are built from `__file__` and
    the comparison never reads the packaging configuration the test is named for.
    """
    assert not (REPO_ROOT / "scripts" / "__init__.py").exists()

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    roots = {pyproject["tool"]["setuptools"]["package-dir"][""]}
    roots.update(package["from"] for package in pyproject["tool"]["poetry"]["packages"])

    assert roots == {"src"}, f"a packaging root outside src/ would distribute the rig: {sorted(roots)}"
