"""
Stamp the machine's load into an artifact, so a contended run says so itself.

This repository's threading tests have wall-clock bounds, so a busy machine can
fail a run that passes on an idle one. A printed load stamp lets a later reader
discard a contended result on evidence.

Run `python scripts/quiet_box.py` to print a stamp for now. `scripts/` is not a
package, so a consumer elsewhere loads this module by path; `load_quiet_box`
cannot do that bootstrapping for it.
"""

from __future__ import annotations

import importlib.util
import os

from pathlib import Path
from types import ModuleType

# Above this, re-run a wall-clock-bounded result before trusting it. At 1.0 the
# run queue is as long as the machine has cores. This only labels a run.
QUIET_BOX_LOAD_PER_CORE = 1.0


def load_per_core() -> float | None:
    """
    Report the 1-minute load average divided by the CPU count.

    Returns:
        float | None: Load per core, or None if the platform reports no load
        average or CPU count. None means unknown, never quiet.

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
        bool: True only when the load per core is known and at or below
        `QUIET_BOX_LOAD_PER_CORE`.

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
        quiet-box verified.

    """
    per_core = load_per_core()
    if per_core is None:
        return f"QUIET BOX [{label}]: load average unavailable - NOT quiet-box verified"
    verdict = "quiet-box verified" if per_core <= QUIET_BOX_LOAD_PER_CORE else "NOT quiet-box verified"
    return f"QUIET BOX [{label}]: {per_core:.2f} load per core (threshold {QUIET_BOX_LOAD_PER_CORE:.2f}) - {verdict}"


def load_quiet_box(start: Path) -> ModuleType:
    """
    Find and execute this module afresh, given any path inside the repository.

    A caller must already hold this module to call this, so a consumer that needs to
    bootstrap resolves the path itself, as `scenario_file_swap_barrier.py` does. A
    script run as `python scripts/<name>.py` can just `import quiet_box`.

    Args:
        start: Any path inside the repository, usually the caller's
            `__file__`.

    Returns:
        ModuleType: This module, freshly executed.

    Raises:
        FileNotFoundError: If no `scripts/quiet_box.py` is found above `start`.

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
