"""
Thin pytest entry point for the `create_camera(from_viewport=True)` gate scenario.

Shells out to `scripts/blender_rig.py` running
`scripts/rig_scenarios/scenario_viewport_camera.py`, with its in-Blender helper
`in_blender_viewport_camera.py`, against a live GUI Blender, so the scenario has one copy.
Imports no `bpy`, so collection works outside Blender.

This is the only gate that reaches `from_viewport`'s actual work: `--background` has no
window and so no `RegionView3D`, so `just smoke` can only prove the refusal and the server
tests only the argument validation.

Runs only with `BLENDERMCP_LIVE_RIG=1`, so the default run never launches Blender. The
skip happens at collection time so the summary shows it.
"""

import os
import subprocess
import sys

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BLENDER = Path("/opt/homebrew/bin/blender")
_LIVE_RIG_ENV_VAR = "BLENDERMCP_LIVE_RIG"
_RIG = _REPO_ROOT / "scripts" / "blender_rig.py"
_SCENARIO = _REPO_ROOT / "scripts" / "rig_scenarios" / "scenario_viewport_camera.py"
_HELPER = _REPO_ROOT / "scripts" / "rig_scenarios" / "in_blender_viewport_camera.py"
# Three views and a handful of file round trips; no rendering.
_SCENARIO_TIMEOUT_SECONDS = 300


def _live_rig_unavailable_reason() -> str | None:
    """
    Say why the live-Blender viewport scenario cannot run here, or None if it can.

    The opt-in env var is checked before whether Blender is installed: the default
    `pytest` run must not launch Blender even on a machine that has one.

    Returns:
        str | None: A human-readable reason to skip, or None to run.

    """
    if os.environ.get(_LIVE_RIG_ENV_VAR) != "1":
        return f"set {_LIVE_RIG_ENV_VAR}=1 to opt in; the default pytest run must not launch a live GUI Blender"
    if not _BLENDER.is_file():
        return f"no Blender found at {_BLENDER} (scripts/blender_rig.py only knows this one path)"
    return None


_SKIP_REASON = _live_rig_unavailable_reason()
if _SKIP_REASON is not None:
    pytest.skip(f"the viewport camera gate needs a live GUI Blender: {_SKIP_REASON}", allow_module_level=True)


@pytest.mark.phase2_gate
def test_a_camera_created_from_the_viewport_sees_what_the_viewport_sees_against_a_live_blender(
    tmp_path: Path,
) -> None:
    """
    Run the viewport-camera scenario through the live rig and assert it passed.

    Args:
        tmp_path: Pytest's per-test scratch directory, holding the rig's work dir and the
            request/answer files the scenario and its in-Blender helper exchange.

    """
    work_dir = tmp_path / "rig"
    work_dir.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            str(_RIG),
            "--work-dir",
            str(work_dir),
            "--scenario",
            str(_SCENARIO),
            "--blender-script",
            str(_HELPER),
        ],
        capture_output=True,
        text=True,
        timeout=_SCENARIO_TIMEOUT_SECONDS,
        check=False,
    )
    print(result.stdout)
    print(result.stderr)
    assert result.returncode == 0, f"the viewport camera scenario did not pass:\n{result.stdout}\n{result.stderr}"
    assert "RIG PASSED" in result.stdout, "the rig exited 0 but never printed its own verdict line"
