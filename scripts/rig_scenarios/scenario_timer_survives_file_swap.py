r"""
Check that the addon keeps serving across a live `wm.open_mainfile`; a `scripts/blender_rig.py` scenario.

From the repository root::

    blender --background --factory-startup \
        --python scripts/rig_scenarios/make_fixture.py -- <work>/fixture.blend
    .venv/bin/python scripts/blender_rig.py \
        --work-dir <work>/rig \
        --scenario scripts/rig_scenarios/scenario_timer_survives_file_swap.py \
        --blender-script scripts/rig_scenarios/in_blender_open_mainfile.py \
        --blend fixture=<work>/fixture.blend

Needs a GUI Blender: the addon's server refuses to start under `--background`. This
scenario runs in the rig's process without `bpy`; the Blender side is the `--blender-script`.

Checks ping and handshake before and after the load, the load itself, and which timers
survive it. Fails by raising; a clean return is a pass.
"""

import json
import time

from pathlib import Path
from typing import Protocol

from blender_mcp.addon_manager import EXPECTED_ADDON_PROTOCOL_VERSION


class Rig(Protocol):
    """
    The part of `BlenderRig` a scenario may use.

    A Protocol because the rig loads scenarios by path; importing the rig here would
    reverse that dependency.
    """

    work_dir: Path
    blends: dict[str, Path]

    def send(self, command_type: str, params: dict | None = None) -> dict:
        """
        Send one addon command and return its decoded response.

        Args:
            command_type: The addon command name, e.g. "ping".
            params: Command parameters; omitted means none.

        Returns:
            dict: The decoded response, including the echoed request id.

        """
        ...


# Generous, because a slow host would otherwise fail as if the addon were broken.
DEADLINE_SECONDS = 120.0
POLL_SECONDS = 0.25


def _await_file(path: Path, what: str) -> dict:
    """
    Block until the in-Blender half writes `path`, then decode it.

    Args:
        path: File the Blender-side script is expected to produce.
        what: Human name for it, used in the failure message.

    Returns:
        dict: The decoded JSON document.

    Raises:
        AssertionError: If the file never appeared within the deadline, meaning
            the in-Blender half never ran.

    """
    limit = time.monotonic() + DEADLINE_SECONDS
    while time.monotonic() < limit:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        time.sleep(POLL_SECONDS)
    raise AssertionError(f"{what} never appeared at {path}")


def _await_another_fire(heartbeat: Path, before: int) -> int:
    """
    Wait for the persistent timer's fire count to move past `before`.

    A timer can stay registered after the swap and never fire again.

    Args:
        heartbeat: The heartbeat document the in-Blender half rewrites each fire.
        before: The count captured inside the swap callback.

    Returns:
        int: The first count observed above `before`.

    Raises:
        AssertionError: If the timer never fired again within the deadline.

    """
    limit = time.monotonic() + DEADLINE_SECONDS
    while time.monotonic() < limit:
        now = json.loads(heartbeat.read_text(encoding="utf-8"))["persistent"]
        if now > before:
            return now
        time.sleep(POLL_SECONDS)
    raise AssertionError(f"the persistent timer never fired again after the swap (stuck at {before})")


def run(rig: Rig) -> None:
    """
    Execute the scenario; see the module docstring.

    Args:
        rig: The `BlenderRig` the harness built, already serving.

    """
    assert rig.send("ping")["result"]["pong"] is True, "pre-load ping did not come back"

    info = rig.send("get_addon_info")["result"]
    print(f"RIG: protocol_version = {info['protocol_version']}", flush=True)
    print(f"RIG: blender_version = {info['blender_version']}", flush=True)
    print(f"RIG: writable_output_roots = {info['writable_output_roots']}", flush=True)
    assert info["protocol_version"] == EXPECTED_ADDON_PROTOCOL_VERSION, (
        f"expected protocol {EXPECTED_ADDON_PROTOCOL_VERSION}, got {info['protocol_version']}"
    )
    assert info["writable_output_roots"], "handshake carried no writable_output_roots"

    heartbeat = rig.work_dir / "heartbeat.json"
    _await_file(heartbeat, "the first heartbeat")

    fixture = rig.blends["fixture"]
    (rig.work_dir / "open_now.json").write_text(json.dumps({"filepath": str(fixture)}), encoding="utf-8")
    print(f"RIG: asked Blender to open {fixture}", flush=True)

    outcome = _await_file(rig.work_dir / "outcome.json", "the in-Blender outcome")
    print("RIG: in-Blender outcome = " + json.dumps(outcome, indent=2), flush=True)

    assert outcome["open_mainfile_result"] == ["FINISHED"], outcome["open_mainfile_result"]
    assert outcome["before_objects"] != outcome["after_objects"], "the swap was a no-op"
    assert outcome["after_objects"] == ["RigFixtureCube"], outcome["after_objects"]
    assert outcome["after_filepath"] == str(fixture), outcome["after_filepath"]
    assert outcome["after_persistent_timer_registered"] is True, "the persistent timer did not survive"
    assert outcome["after_volatile_timer_registered"] is False, "the control timer survived; the probe is blind"
    assert outcome["callback_frame_survived_the_swap"] is True

    before = outcome["after_persistent_fires"]
    after = _await_another_fire(heartbeat, before)
    print(f"RIG: persistent timer fires {before} -> {after} after the swap", flush=True)

    post = rig.send("ping")
    assert post["result"]["pong"] is True, "post-load ping did not come back"
    print(f"RIG: post-load ping answered -> {json.dumps(post)}", flush=True)

    after_info = rig.send("get_addon_info")["result"]
    print(f"RIG: post-load get_addon_info protocol_version = {after_info['protocol_version']}", flush=True)
    print(f"RIG: post-load writable_output_roots = {after_info['writable_output_roots']}", flush=True)
    assert after_info["protocol_version"] == EXPECTED_ADDON_PROTOCOL_VERSION
    print("RIG PASSED", flush=True)
