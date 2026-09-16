"""
Stamp the machine's load into an artifact, so a contended run says so itself.

**Why this exists.** This repository's threading tests are wall-clock bounded.
Measured on 2026-09-16: the suite reports `907 passed` on four consecutive runs
on an idle box, and `906 / 905 / 905 / 907` across four consecutive runs on the
*same tree* while three other agents saturated the machine. A reviewer reading
only the second set concluded the suite was "not reproducibly green" - a wrong
conclusion that cost four full re-runs to disprove, after the fact, from memory.

The fix is not a better forecast of when the box will be quiet. It is for the
run to record the conditions it ran under, so a later reader can discard a
contended result on evidence instead of reconstructing the machine's history.
The pattern is `timogin-1`'s: they stamped load-per-core around a paired latency
measurement and caught a 29.5 ms -> 47.6 ms inflation (1.6x, same code, same
fixture) that was otherwise invisible, because 47.6 ms is a plausible number.

`scripts/` is deliberately not a package (see `tests/test_blender_rig.py`), so a
consumer outside this directory loads this module by path rather than importing
it. `load_quiet_box()` below is that loader, kept here so the two call sites do
not each invent one.

This module touches no repository state and mutates no global: it reads
`os.getloadavg()` and `os.cpu_count()` and formats a string.
"""

from __future__ import annotations

import importlib.util
import os

from pathlib import Path
from types import ModuleType

# Above this, a wall-clock-bounded result is not trustworthy without a re-run.
# 1.0 per core means "the run queue is as long as the machine is wide"; the
# suite's own bounds carry roughly 0.75 s of margin against a 2.0 s stimulus, so
# a queue at parity is already enough to eat it. This is a reporting threshold,
# not an enforcement one - nothing here fails a run, it only labels it.
QUIET_BOX_LOAD_PER_CORE = 1.0


def load_per_core() -> float | None:
    """
    Report the 1-minute load average divided by the CPU count.

    Returns:
        float | None: Load per core, or None where the platform does not report
        a load average (`os.getloadavg` raises `OSError` on some) or the CPU
        count is unavailable. None means "unknown", never "quiet".

    """
    try:
        one_minute = os.getloadavg()[0]
    except (OSError, AttributeError):
        return None
    cores = os.cpu_count()
    if not cores:
        return None
    return one_minute / cores


def is_quiet() -> bool:
    """
    Report whether the box is quiet enough to trust a wall-clock-bounded result.

    Returns:
        bool: True only when a load average was readable **and** is at or below
        `QUIET_BOX_LOAD_PER_CORE`. An unreadable load average returns False, so
        an unknown machine is never labelled verified.

    """
    per_core = load_per_core()
    return per_core is not None and per_core <= QUIET_BOX_LOAD_PER_CORE


def stamp(label: str) -> str:
    """
    Build the one-line load stamp an artifact prints.

    Args:
        label: Where in the run this was taken, e.g. "before" or "after".

    Returns:
        str: A single line naming the load per core and whether the result is
        quiet-box verified. Callers print it; this function performs no I/O so a
        test can assert on the text.

    """
    per_core = load_per_core()
    if per_core is None:
        return f"QUIET BOX [{label}]: load average unavailable - NOT quiet-box verified"
    verdict = "quiet-box verified" if per_core <= QUIET_BOX_LOAD_PER_CORE else "NOT quiet-box verified"
    return f"QUIET BOX [{label}]: {per_core:.2f} load per core (threshold {QUIET_BOX_LOAD_PER_CORE:.2f}) - {verdict}"


def load_quiet_box(start: Path) -> ModuleType:
    """
    Find and execute this module afresh, given any path inside the repository.

    **This cannot bootstrap a consumer that has no access to this module**, and
    an earlier version of this docstring claimed it could. It is defined *in*
    the module it loads, so anything able to call it already holds the module -
    the chicken-and-egg was a design error, caught by the rig scenario that tried
    to use it for exactly the job the old text advertised. A consumer that must
    bootstrap resolves the path itself; `scripts/rig_scenarios/
    scenario_file_swap_barrier.py` is the worked example and says so.

    What it is good for is a caller that already has the module and wants a
    *fresh* execution located from an arbitrary starting path - a test that
    exercises the search, or a tool run from a subdirectory. A script run as
    `python scripts/<name>.py` has `scripts/` as `sys.path[0]` and should just
    `import quiet_box`.

    Args:
        start: Any path inside the repository, typically the caller's
            `__file__`. The search walks upwards for `scripts/quiet_box.py`.

    Returns:
        ModuleType: This module, freshly executed.

    Raises:
        FileNotFoundError: If no `scripts/quiet_box.py` is found above `start`,
            which means the caller was copied out of the repository.

    """
    for parent in Path(start).resolve().parents:
        candidate = parent / "scripts" / "quiet_box.py"
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("quiet_box_by_path", candidate)
            if spec is None or spec.loader is None:
                break
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise FileNotFoundError(f"scripts/quiet_box.py not found above {start}")


if __name__ == "__main__":
    print(stamp("now"))
