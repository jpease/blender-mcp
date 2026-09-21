"""
Thin pytest entry point for the render-overhead measurement scenario.

Shells out to `scripts/blender_rig.py` running
`scripts/rig_scenarios/scenario_render_overhead.py`, so the scenario has one copy. Imports
no `bpy` and no MCP client, so collection works outside Blender.

This is a measurement, not an equality check. The scenario renders the same 240-frame range
through both `render_scene(mode="ANIMATION")` implementations - the default per-frame
orchestration and the single-call `orchestrate_animation=False` path - and fails only if the
per-frame difference exceeds a ceiling far above the measured figure. What it defends is the
claim that the default's convenience (progress notifications, cancellation between frames)
does not cost a production shot minutes of wall clock; the number it prints is the record.

Runs only with `BLENDERMCP_LIVE_RIG=1`, so the default run never launches Blender. The skip
happens at collection time so the summary shows it.
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
_SCENARIO = _REPO_ROOT / "scripts" / "rig_scenarios" / "scenario_render_overhead.py"
_SERVER_BINARY = _REPO_ROOT / ".venv" / "bin" / "blender-mcp"
# Four warm-up frames plus 4 x 240 measured 64px Workbench frames; the observed run is ~11s.
_SCENARIO_TIMEOUT_SECONDS = 900


def _live_rig_unavailable_reason() -> str | None:
    """
    Say why the live MCP-client scenario cannot run here, or None if it can.

    The opt-in env var is checked before whether Blender is installed: the default `pytest`
    run must not launch Blender even on a machine that has one. The console script is
    checked too, because the scenario spawns the shipped `blender-mcp` entry point rather
    than importing the server in-process.

    Returns:
        str | None: A human-readable reason to skip, or None to run.

    """
    if os.environ.get(_LIVE_RIG_ENV_VAR) != "1":
        return f"set {_LIVE_RIG_ENV_VAR}=1 to opt in; the default pytest run must not launch a live GUI Blender"
    if not _BLENDER.is_file():
        return f"no Blender found at {_BLENDER} (scripts/blender_rig.py only knows this one path)"
    if not _SERVER_BINARY.is_file():
        return f"no server console script at {_SERVER_BINARY}; install this project into .venv first"
    return None


_SKIP_REASON = _live_rig_unavailable_reason()
if _SKIP_REASON is not None:
    pytest.skip(f"the render overhead gate needs a live GUI Blender: {_SKIP_REASON}", allow_module_level=True)


@pytest.mark.phase2_gate
def test_orchestrating_an_animation_per_frame_stays_cheap_against_a_live_blender(tmp_path: Path) -> None:
    """
    Run the render-overhead scenario through the live rig and assert it passed.

    Args:
        tmp_path: Pytest's per-test scratch directory, holding the rig's work dir and the
            frames the scenario renders.

    """
    work_dir = tmp_path / "rig"
    work_dir.mkdir()
    result = subprocess.run(
        [sys.executable, str(_RIG), "--work-dir", str(work_dir), "--scenario", str(_SCENARIO)],
        capture_output=True,
        text=True,
        timeout=_SCENARIO_TIMEOUT_SECONDS,
        check=False,
    )
    print(result.stdout)
    print(result.stderr)
    assert result.returncode == 0, f"the render overhead scenario did not pass:\n{result.stdout}\n{result.stderr}"
    assert "RIG PASSED" in result.stdout, "the rig exited 0 but never printed its own verdict line"
