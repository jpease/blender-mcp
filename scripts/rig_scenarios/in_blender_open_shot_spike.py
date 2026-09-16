"""
Graft an `open_shot`-shaped command onto the running addon, for the rig's Task 3 Step 7.

Runs inside Blender as a `--blender-script`, after `scripts/blender_rig.py`'s
bootstrap has put a server on `bpy.types.blendermcp_server`. `open_shot` itself is
Task 6's; what Task 3 needs live is a command that *is* a session swap, so the
production barrier, epoch and transaction-bypass paths run for real.

**Nothing here shadows `_READ_ONLY_COMMANDS`.** Task 2's spike had to, because
`_run_handler` would otherwise have wrapped the swap in `mutation_transaction` and
triggered the data-destruction bug Task 4 has not fixed. It no longer has to:
`open_shot` is in `_SESSION_SWAP_COMMANDS`, and `_run_handler` bypasses the
transaction for that set. The bypass under test is production code, not a prop -
which is the point of running this live.

**The failure path is scrubbed here, deliberately.** Blender's own
`RuntimeError` text embeds the absolute path, up to twice in one message; Task 6
owns the sanitizer. This spike raises its own text instead, so the transcript
this produces cannot be read as evidence that the real `open_shot` will be
leak-free. What the transcript *does* prove about paths is narrower and is
Task 3's own: the **barrier's** rejection carries none.
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
    Swap the database, the way Task 6's `open_shot` will.

    Args:
        filepath: The .blend to open. Passed straight to the operator; this
            spike does no validation, which is Task 5's boundary.
        probe_delay: Seconds to stall on Blender's main thread **before** the
            operator runs. It exists to make the mid-load window observable at
            human timescale: `wm.open_mainfile` was measured at 4.6 s on a
            1.05 GB fixture, and the rig's fixture loads in milliseconds, so
            without this the scenario could never get a frame into the queue
            between the pre-swap snapshot and `load_post`. The delay changes
            *when* the epoch moves, not *whether* - which is the whole property
            under test.

    Returns:
        dict: The new filepath and object list, so the caller can see which
        database answered.

    Raises:
        RuntimeError: If Blender could not open the file. The message is this
            spike's own, never Blender's - see the module docstring.

    """
    if probe_delay:
        time.sleep(float(probe_delay))
    try:
        # use_scripts=False is passed explicitly: spec Decision #7 forbids
        # arbitrary code execution, and the operator's own default does not
        # guarantee it.
        bpy.ops.wm.open_mainfile(filepath=filepath, use_scripts=False)
    except RuntimeError:
        raise RuntimeError("the open_shot spike could not open the requested .blend") from None
    return {
        "filepath": bpy.data.filepath,
        "objects": sorted(obj.name for obj in bpy.data.objects),
    }


def _probe_a_timer_that_raises() -> None:
    """
    Answer, live, whether Blender drops a `bpy.app.timers` callback that raises.

    This decides how bad `_execute_and_answer`'s `BaseException` handling has to
    be. If Blender unregisters a callback that raised, then one escaping
    exception kills `drain_command_queue` **permanently** and every connected
    client hangs forever - a far worse failure than the one abort that caused
    it. It cannot be measured under `--background`, where timers never fire at
    all, which is why it lives here rather than in
    `scripts/blender_probes/session_handlers.py`.

    The callback raises on its **first** call only and returns an interval
    afterwards, so "dropped" and "returned None" cannot be confused: if Blender
    keeps it, the call count climbs past 1.

    It also registers a **successor** - a different function object - immediately
    before raising, and the report says whether that successor survived. That is
    the question behind `drain_command_queue`'s recovery path: if a dying
    callback can hand off to a fresh one, an escaping exception costs one tick
    rather than the whole server.
    """
    calls: list[int] = []
    successor_calls: list[int] = []

    def _successor() -> float:
        """
        Stand in for a *fresh* drain callback registered from inside the failing one.

        Returns:
            float: The next interval, so a surviving successor keeps ticking and
            the count distinguishes "alive" from "ran once and died too".

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
            # Registered from the main thread, which is where a timer callback
            # runs, and as a *different* object so Blender's drop of `_raiser`
            # cannot take it too. Whether that actually works is the question.
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

    # `persistent=True` on both, and both well before the scenario's first
    # swap. A non-persistent timer is unregistered by *any* file load, which on
    # the first attempt silently killed the observer before it could report and
    # would have made "dropped by the raise" indistinguishable from "dropped by
    # the load". (The production drain timer is registered `persistent=True`
    # for the same reason - `_register_drain_timer`.)
    bpy.app.timers.register(_raiser, first_interval=0.05, persistent=True)
    bpy.app.timers.register(_observer, first_interval=0.6, persistent=True)


def _install() -> None:
    """Wrap the running server's handler table so it advertises and dispatches `open_shot`."""
    server = bpy.types.blendermcp_server
    build_handlers = server._build_command_handlers

    def _with_open_shot() -> dict:
        """
        Return the real handler table plus the spike's own entry.

        Returns:
            dict: The command table `get_addon_info` advertises and
            `execute_command_internal` dispatches through.

        """
        handlers = build_handlers()
        handlers["open_shot"] = _open_shot
        return handlers

    server._build_command_handlers = _with_open_shot
    SPIKE_READY.write_text(
        json.dumps(
            {
                "advertised": "open_shot" in server.get_addon_info()["capabilities"],
                "in_session_swap_commands": "open_shot" in server._SESSION_SWAP_COMMANDS,
                "shadowed_read_only_commands": False,
            }
        ),
        encoding="utf-8",
    )
    print("SPIKE: open_shot installed, read-only shadowing: False", flush=True)


_install()
_probe_a_timer_that_raises()
