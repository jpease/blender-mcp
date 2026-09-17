"""
Probe which timers survive a main-thread `wm.open_mainfile`, for `scenario_timer_survives_file_swap.py`.

Runs inside Blender as a `--blender-script`, after the rig's bootstrap starts the
addon's socket server. Registers a persistent heartbeat, a non-persistent control, and
a watcher that waits for the rig's `open_now.json`, opens the file from the main thread,
and writes both sides of the load to `outcome.json`.

The load is a direct operator call, not a queued socket command, so this says nothing
about queued commands across a swap.
"""

import json
import os
import pathlib

import bpy

WORK = pathlib.Path(os.environ["BLENDERMCP_RIG_WORK_DIR"])
OPEN_NOW = WORK / "open_now.json"
OUTCOME = WORK / "outcome.json"
HEARTBEAT = WORK / "heartbeat.json"

_fires = {"persistent": 0, "volatile": 0}


def _write_heartbeat() -> None:
    """Publish the fire counts so the out-of-process scenario can poll them."""
    HEARTBEAT.write_text(json.dumps(_fires), encoding="utf-8")


def _persistent_beat() -> float:
    """
    Count one fire of the persistent timer, which should survive the swap.

    Returns:
        float: The interval Blender should wait before calling again.

    """
    _fires["persistent"] += 1
    _write_heartbeat()
    return 0.25


def _volatile_beat() -> float:
    """
    Count one fire of the control timer, which Blender should drop on load.

    If this one survived too, the persistent timer surviving would prove nothing.

    Returns:
        float: The interval Blender should wait before calling again.

    """
    _fires["volatile"] += 1
    _write_heartbeat()
    return 0.25


def _snapshot(prefix: str) -> dict:
    """
    Capture the timer and database state under a `before_`/`after_` key prefix.

    Args:
        prefix: Either "before" or "after".

    Returns:
        dict: One key per observation, prefixed for the outcome document.

    """
    server = bpy.types.blendermcp_server
    return {
        f"{prefix}_filepath": bpy.data.filepath,
        f"{prefix}_objects": sorted(o.name for o in bpy.data.objects),
        f"{prefix}_persistent_timer_registered": bpy.app.timers.is_registered(_persistent_beat),
        f"{prefix}_volatile_timer_registered": bpy.app.timers.is_registered(_volatile_beat),
        f"{prefix}_persistent_fires": _fires["persistent"],
        f"{prefix}_volatile_fires": _fires["volatile"],
        # Always False, registered or not: each access builds a new bound method and
        # `is_registered` matches by identity. Recorded so the outcome keeps checking it.
        f"{prefix}_drain_is_registered_fresh_bound_method": bpy.app.timers.is_registered(server.drain_command_queue),
    }


def _watch() -> float | None:
    """
    Poll for the rig's go-ahead, then swap the file and record both sides of it.

    Returns:
        float | None: The poll interval while waiting, or None once the swap has
            been recorded, which unregisters this timer.

    """
    if not OPEN_NOW.is_file():
        return 0.25
    target = json.loads(OPEN_NOW.read_text(encoding="utf-8"))["filepath"]
    outcome = _snapshot("before")
    outcome["open_mainfile_result"] = sorted(bpy.ops.wm.open_mainfile(filepath=target, use_scripts=False))
    outcome.update(_snapshot("after"))
    # Reached only if this callback's frame outlived the database swap.
    outcome["callback_frame_survived_the_swap"] = True
    OUTCOME.write_text(json.dumps(outcome, indent=2), encoding="utf-8")
    print("RIG: in-Blender outcome written to", OUTCOME, flush=True)
    return None


bpy.app.timers.register(_persistent_beat, persistent=True)
bpy.app.timers.register(_volatile_beat, persistent=False)
bpy.app.timers.register(_watch, persistent=True)
print("RIG-BLENDER: watcher + probe timers registered, waiting for", OPEN_NOW, flush=True)
