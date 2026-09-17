"""
Thin pytest entry point for the phase-2 gate scenario.

Shells out to `scripts/blender_rig.py` running
`scripts/rig_scenarios/scenario_phase2_gate.py` against a live GUI Blender, so the
scenario has one copy. Imports no `bpy`, so collection works outside Blender.

Runs only with `BLENDERMCP_LIVE_RIG=1`, so the default run never launches Blender.
The skip happens at collection time so the summary shows it, unlike the
`tests/blender_*_smoke.py` files pytest never collects.
"""

import os
import subprocess
import sys

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BLENDER = Path("/opt/homebrew/bin/blender")
_LIVE_RIG_ENV_VAR = "BLENDERMCP_LIVE_RIG"
_MAKE_FIXTURES = _REPO_ROOT / "scripts" / "rig_scenarios" / "make_phase2_gate_fixtures.py"
_RIG = _REPO_ROOT / "scripts" / "blender_rig.py"
_SCENARIO = _REPO_ROOT / "scripts" / "rig_scenarios" / "scenario_phase2_gate.py"
_COMPRESSED_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "blend" / "empty_zstd.blend"
_FIXTURE_BUILD_TIMEOUT_SECONDS = 120
_SCENARIO_TIMEOUT_SECONDS = 600


def _live_rig_unavailable_reason() -> str | None:
    """
    Say why the live-Blender gate scenario cannot run here, or None if it can.

    The opt-in env var is checked before whether Blender is installed: the
    default `pytest` run must not launch Blender even on a machine that has one.

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
    pytest.skip(f"phase-2 gate scenario needs a live GUI Blender: {_SKIP_REASON}", allow_module_level=True)


@pytest.mark.phase2_gate
def test_phase2_gate_scenario_passes_against_a_live_blender(tmp_path: Path) -> None:
    """
    Build the two fixtures, run the gate scenario through the live rig, and assert it passed.

    Args:
        tmp_path: Pytest's per-test scratch directory, holding the rig's work
            dir and both fixtures.

    """
    canon = tmp_path / "canon.blend"
    shot = tmp_path / "shot.blend"
    build = subprocess.run(
        [
            str(_BLENDER),
            "--background",
            "--factory-startup",
            "--python",
            str(_MAKE_FIXTURES),
            "--",
            str(canon),
            str(shot),
        ],
        capture_output=True,
        text=True,
        timeout=_FIXTURE_BUILD_TIMEOUT_SECONDS,
        check=False,
    )
    assert build.returncode == 0, f"fixture build failed:\n{build.stdout}\n{build.stderr}"
    assert canon.is_file(), f"canon fixture was not written:\n{build.stdout}\n{build.stderr}"
    assert shot.is_file(), f"shot fixture was not written:\n{build.stdout}\n{build.stderr}"

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
            "--blend",
            f"canon={canon}",
            "--blend",
            f"shot={shot}",
            "--blend",
            f"compressed={_COMPRESSED_FIXTURE}",
        ],
        capture_output=True,
        text=True,
        timeout=_SCENARIO_TIMEOUT_SECONDS,
        check=False,
    )
    print(result.stdout)
    print(result.stderr)
    assert result.returncode == 0, f"the phase-2 gate scenario did not pass:\n{result.stdout}\n{result.stderr}"
    assert "RIG PASSED" in result.stdout, "the rig exited 0 but never printed its own verdict line"
