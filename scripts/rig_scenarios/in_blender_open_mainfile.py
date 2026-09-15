"""
Probe timer persistence across a main-thread `wm.open_mainfile`, for the rig's Step 5.

Runs inside Blender as a `--blender-script`, after the rig's bootstrap has started
the addon's socket server. Registers three timers: a `persistent=True` heartbeat, a
`persistent=False` control that must *not* survive the swap, and a watcher that polls
for the rig's `open_now.json` and then performs the swap from the main thread.

The swap is a scripted operator call, deliberately not a queued socket command: what
Task 2 decides about queued commands is a separate question this script does not ask.
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
    Count one fire of the persistent timer; this one must survive the swap.

    Returns:
        float: The interval Blender should wait before calling again.

    """
    _fires["persistent"] += 1
    _write_heartbeat()
    return 0.25


def _volatile_beat() -> float:
    """
    Count one fire of the control timer; Blender must drop this one on load.

    It exists to make the persistent timer's survival mean something: if this one
    survived too, the probe could not tell the two dispositions apart.

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
        # Always False, registered or not: `obj.method` builds a fresh bound method
        # on every access and `is_registered` matches on identity. Recorded to keep
        # the 5.2.1 finding under measurement rather than in prose.
        f"{prefix}_drain_is_registered_fresh_bound_method": bpy.app.timers.is_registered(server.drain_command_queue),
    }


def _watch() -> float | None:
    """
    Poll for the rig's go-ahead, then swap the file and record both sides of it.

    Returns:
        float | None: The poll interval while waiting, or None once the swap has
            been recorded, which unregisters this timer from the inside.

    """
    if not OPEN_NOW.is_file():
        return 0.25
    target = json.loads(OPEN_NOW.read_text(encoding="utf-8"))["filepath"]
    outcome = _snapshot("before")
    outcome["open_mainfile_result"] = sorted(bpy.ops.wm.open_mainfile(filepath=target, use_scripts=False))
    outcome.update(_snapshot("after"))
    # Reached at all only if this callback's frame outlived the database swap.
    outcome["callback_frame_survived_the_swap"] = True
    OUTCOME.write_text(json.dumps(outcome, indent=2), encoding="utf-8")
    print("RIG: in-Blender outcome written to", OUTCOME, flush=True)
    return None


bpy.app.timers.register(_persistent_beat, persistent=True)
bpy.app.timers.register(_volatile_beat, persistent=False)
bpy.app.timers.register(_watch, persistent=True)
print("RIG-BLENDER: watcher + probe timers registered, waiting for", OPEN_NOW, flush=True)
