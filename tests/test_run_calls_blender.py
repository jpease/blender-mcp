"""
`blender-mcp run-calls` end to end against a real headless Blender.

Skipped unless `BLENDERMCP_TEST_BLENDER` names a Blender executable; CI has none. Each test runs
the CLI the way a user does, so the toolset re-execution, the bootstrap, `serve_in_background`
and the in-process tool layer are all exercised together.
"""

import json
import os
import re
import subprocess
import sys

from pathlib import Path

import pytest

BLENDER = os.environ.get("BLENDERMCP_TEST_BLENDER", "")
TWO_CALLS = Path(__file__).parent / "fixtures" / "run_calls" / "two_calls.json"
# Generous: each run starts a Blender, and the first start on a machine can be slow.
RUN_TIMEOUT_SECONDS = 300

pytestmark = [
    pytest.mark.run_calls_blender,
    pytest.mark.skipif(
        not (BLENDER and os.path.isfile(BLENDER) and os.access(BLENDER, os.X_OK)),
        reason="BLENDERMCP_TEST_BLENDER does not name a Blender executable",
    ),
]


def _run_calls(*args: str) -> subprocess.CompletedProcess[str]:
    """
    Run `blender-mcp run-calls` through the CLI entry point.

    Args:
        *args: The arguments after `run-calls`; `--blender` is added.

    Returns:
        subprocess.CompletedProcess[str]: The finished run.

    """
    return subprocess.run(
        [sys.executable, "-c", "from blender_mcp.server import main; main()", "run-calls", "--blender", BLENDER, *args],
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_SECONDS,
        check=False,
    )


def _write_calls(path: Path, calls: list[dict]) -> Path:
    path.write_text(json.dumps(calls), encoding="utf-8")
    return path


def _bootstrap_pids() -> set[int]:
    """
    List the Blenders running the run-calls bootstrap.

    Returns:
        set[int]: Their pids.

    """
    listing = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=True).stdout
    return {
        int(line.split(None, 1)[0])
        for line in listing.splitlines()
        if "run_calls_bootstrap.py" in line and "--python" in line
    }


def test_two_calls_succeed_and_no_blender_is_left() -> None:
    before = _bootstrap_pids()

    run = _run_calls(str(TWO_CALLS))

    assert run.returncode == 0, run.stderr
    lines = run.stdout.splitlines()
    assert [json.loads(line)["ok"] for line in lines[:-1]] == [True, True]
    assert lines[-1] == '{"applied": 2, "total": 2}'
    ready = re.search(r"^BLENDERMCP_RUN_CALLS_READY \d+$", run.stderr, re.MULTILINE)
    assert ready, run.stderr
    assert _bootstrap_pids() <= before


@pytest.mark.parametrize("name", ["Cube", "Camera", "Light"])
def test_without_open_the_calls_start_from_an_empty_scene(tmp_path: Path, name: str) -> None:
    calls = _write_calls(tmp_path / "calls.json", [{"tool": "get_object_info", "args": {"object_name": name}}])

    run = _run_calls(str(calls))

    assert run.returncode == 1, run.stderr
    first, summary = run.stdout.splitlines()
    outcome = json.loads(first)
    assert outcome["ok"] is False
    assert f"Object not found: {name}" in outcome["error"]
    assert summary == '{"applied": 0, "total": 1}'


def test_open_keeps_the_files_objects(tmp_path: Path) -> None:
    blend = tmp_path / "factory.blend"
    subprocess.run(
        [
            BLENDER,
            "-b",
            "--factory-startup",
            "--python-expr",
            "import bpy, sys; bpy.ops.wm.save_as_mainfile(filepath=sys.argv[-1])",
            "--",
            str(blend),
        ],
        capture_output=True,
        timeout=RUN_TIMEOUT_SECONDS,
        check=True,
    )
    calls = _write_calls(tmp_path / "calls.json", [{"tool": "get_object_info", "args": {"object_name": "Cube"}}])

    run = _run_calls("--open", str(blend), str(calls))

    assert run.returncode == 0, run.stderr
    first, summary = run.stdout.splitlines()
    assert json.loads(first)["ok"] is True
    assert summary == '{"applied": 1, "total": 1}'
