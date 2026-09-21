"""
Graft an `open_shot`-shaped command onto the running addon, for `scenario_file_swap_barrier.py`.

Runs inside Blender as a `--blender-script`, after `scripts/blender_rig.py`'s
bootstrap has put a server on `bpy.types.blendermcp_server`. Any session-swap command
exercises the production barrier, epoch and transaction bypass, so the real `open_shot`
is not needed. `CommandSpec.read_only` is left alone, so the bypass that runs is the one
`_run_handler` gives a `session_swap` command (a transaction's rollback would remove the
loaded file's data).

On failure the spike raises its own text, not Blender's, which embeds the absolute path.
So the transcript shows only that the barrier's rejection carries no path, not that the
real `open_shot` sanitizes its errors.

Also probes whether Blender drops a timer callback that raises.
"""

import json
import os
import pathlib
import time

import bpy

WORK = pathlib.Path(os.environ["BLENDERMCP_RIG_WORK_DIR"])
SPIKE_READY = WORK / "open_shot_installed.json"
TIMER_PROBE = WORK / "timer_raise_probe.json"


def _open_shot(filepath: str = "", probe_delay: float = 0.0) -> dict:
    """
    Swap the database, the way the real `open_shot` does.

    Args:
        filepath: The .blend to open, passed to the operator unvalidated.
        probe_delay: Seconds to stall on the main thread before the load. The rig's
            fixture loads in milliseconds, too fast for the scenario to queue a frame
            between the pre-swap snapshot and `load_post`; the delay widens that
            window without changing whether the epoch moves.

    Returns:
        dict: The new filepath and object list, so the caller can see which
        database answered.

    Raises:
        RuntimeError: If Blender could not open the file, with the spike's own
            message.

    """
    if probe_delay:
        time.sleep(float(probe_delay))
    try:
        # Explicit: the addon must never run a file's scripts, and the operator's
        # default does not guarantee that.
        bpy.ops.wm.open_mainfile(filepath=filepath, use_scripts=False)
    except RuntimeError:
        raise RuntimeError("the open_shot spike could not open the requested .blend") from None
    return {
        "filepath": bpy.data.filepath,
        "objects": sorted(obj.name for obj in bpy.data.objects),
    }


def _probe_a_timer_that_raises() -> None:
    """
    Record in `timer_raise_probe.json` whether Blender drops a timer callback that raises.

    If it does, one exception escaping `drain_command_queue` ends the drain loop and
    every client hangs. Timers never fire under `--background`, so the headless
    probes cannot ask this.

    The callback raises only on its first call, so a count above 1 means Blender
    kept it. Just before raising it registers a successor, a different function
    object; if that keeps ticking, a dying drain callback can hand off to a new one.
    """
    calls: list[int] = []
    successor_calls: list[int] = []

    def _successor() -> float:
        """
        Stand in for a new drain callback registered from inside the failing one.

        Returns:
            float: The next interval, so a live successor's count keeps climbing.

        """
        successor_calls.append(1)
        return 0.05

    def _raiser() -> float | None:
        """
        Raise once, then behave like an ordinary repeating timer.

        Returns:
            float | None: The next interval, on every call after the first.

        Raises:
            RuntimeError: On the first call only.

        """
        calls.append(1)
        if len(calls) == 1:
            # Legal here: timer callbacks run on the main thread. A separate object,
            # so Blender dropping `_raiser` cannot drop it too.
            bpy.app.timers.register(_successor, first_interval=0.05, persistent=True)
            raise RuntimeError("probe: a timer callback raised")
        return 0.05

    def _observer() -> float | None:
        """
        Record what became of `_raiser` once it has had time to fire repeatedly.

        Returns:
            float | None: None, which unregisters this observer.

        """
        TIMER_PROBE.write_text(
            json.dumps(
                {
                    "calls_after_raising_once": len(calls),
                    "still_registered": bool(bpy.app.timers.is_registered(_raiser)),
                    "successor_calls": len(successor_calls),
                    "successor_still_registered": bool(bpy.app.timers.is_registered(_successor)),
                }
            ),
            encoding="utf-8",
        )
        print(f"SPIKE: timer-after-raise probe: calls={len(calls)}", flush=True)
        return None

    # Persistent, because any file load unregisters a non-persistent timer: the
    # observer could die before reporting, and a drop by the load would look like
    # a drop by the raise.
    bpy.app.timers.register(_raiser, first_interval=0.05, persistent=True)
    bpy.app.timers.register(_observer, first_interval=0.6, persistent=True)


def _install() -> None:
    """Wrap the running server's handler table so it advertises and dispatches `open_shot`."""
    server = bpy.types.blendermcp_server
    build_handlers = server._build_command_handlers

    def _with_open_shot() -> dict:
        """
        Return the real handler table plus the spike's own entry.

        Copied, not mutated: `_build_command_handlers` memoizes its map per
        provider-flag combination, so writing into what it returns would leave the
        spike's entry in the running server's cache after this wrapper is gone.

        Returns:
            dict: The command table `get_addon_info` advertises and
            `execute_command_internal` dispatches through.

        """
        return {**build_handlers(), "open_shot": _open_shot}

    server._build_command_handlers = _with_open_shot
    SPIKE_READY.write_text(
        json.dumps(
            {
                "advertised": "open_shot" in server.get_addon_info()["capabilities"],
                "in_session_swap_commands": server.command_spec("open_shot").session_swap,
                "shadowed_read_only_commands": False,
            }
        ),
        encoding="utf-8",
    )
    print("SPIKE: open_shot installed, read-only shadowing: False", flush=True)


_install()
_probe_a_timer_that_raises()
