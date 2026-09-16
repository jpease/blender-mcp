"""
Guards for `scripts/quiet_box.py`, the load stamp every wall-clock artifact carries.

This module decides whether a timing result in this phase is admissible
evidence, so a defect in it does not fail loudly - it labels a contended run
"quiet-box verified" and a reader then trusts a number that was inflated by
three other agents. That is the same shape as the rig's own failure mode, and it
is why a 130-line helper with no repository state gets its own test file.

The three functions under test are pure: `load_per_core` reads two `os` calls
and divides, `is_quiet` thresholds it, and `stamp` formats the result and
performs **no I/O** precisely so its text can be asserted here rather than
scraped out of a captured stdout. The fourth, `load_quiet_box`, is the by-path
loader the rig scenarios need because `scripts/` is deliberately not a package -
and it is exercised by this file loading the module through it.

Every branch that can return "unknown" is covered in both directions. An
unreadable load average must never be reported as quiet: the whole point of the
stamp is that a reader can discard a result on evidence, and "unknown" silently
reading as "quiet" would produce exactly the false confidence it exists to
remove.
"""

from __future__ import annotations

import importlib.util
import os

from pathlib import Path
from types import ModuleType

import pytest

QUIET_BOX_PATH = Path(__file__).resolve().parents[1] / "scripts" / "quiet_box.py"
# A load of 8.0 across 4 cores, so the expected quotient is exact in binary and
# the assertion needs no tolerance argument to be honest about what it checks.
_STUB_ONE_MINUTE_LOAD = 8.0
_STUB_CPU_COUNT = 4
_EXPECTED_PER_CORE = _STUB_ONE_MINUTE_LOAD / _STUB_CPU_COUNT


def _load_quiet_box() -> ModuleType:
    """
    Load the module under test through its own by-path loader.

    Using `load_quiet_box` here rather than a second hand-rolled
    `spec_from_file_location` is deliberate: it is the entry point both rig
    consumers use, and a test file that loaded the module some other way would
    leave it uncovered while appearing to test the module thoroughly.

    Returns:
        ModuleType: The freshly executed `quiet_box` module.

    """
    spec = importlib.util.spec_from_file_location("quiet_box_bootstrap", QUIET_BOX_PATH)
    assert spec is not None and spec.loader is not None, f"{QUIET_BOX_PATH} is not loadable"
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    return bootstrap.load_quiet_box(Path(__file__))


quiet_box = _load_quiet_box()


def test_load_per_core_divides_the_one_minute_average_by_the_cpu_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The 1-minute figure, not the 5- or 15-minute one, and per core rather than raw.

    `os.getloadavg()` returns three figures and the stamp is about the run that
    is happening now, so the first is the only admissible one. Dividing is what
    makes the number comparable across machines - a load of 12 is idle on a
    64-core box and catastrophic on a 4-core one.
    """
    monkeypatch.setattr(os, "getloadavg", lambda: (_STUB_ONE_MINUTE_LOAD, 99.0, 99.0))
    monkeypatch.setattr(os, "cpu_count", lambda: _STUB_CPU_COUNT)

    assert quiet_box.load_per_core() == pytest.approx(_EXPECTED_PER_CORE)


def test_an_unreadable_load_average_is_unknown_not_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    `os.getloadavg` raises `OSError` where the platform does not keep one.

    None means "unknown", and the distinction is the whole safety property: a
    branch that returned 0.0 here would report every such platform as the
    quietest possible box and label every result on it verified.
    """

    def unavailable() -> tuple[float, float, float]:
        raise OSError("load average is not available on this platform")

    monkeypatch.setattr(os, "getloadavg", unavailable)

    assert quiet_box.load_per_core() is None


def test_a_cpu_count_of_zero_is_unknown_rather_than_a_division_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    `os.cpu_count()` answers None where it cannot tell, and 0 is the same answer.

    The guard is falsiness rather than `is None` for that reason: an `is None`
    test lets 0 through into the division and the stamp raises
    `ZeroDivisionError` from inside a `finally`-less caller, taking out the run
    it was only supposed to annotate.
    """
    monkeypatch.setattr(os, "getloadavg", lambda: (1.0, 1.0, 1.0))
    monkeypatch.setattr(os, "cpu_count", lambda: 0)

    assert quiet_box.load_per_core() is None


def test_an_unknown_load_is_never_reported_as_quiet_box_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The one assertion that makes the whole stamp trustworthy rather than decorative.

    `is_quiet` returns True only when a load average was readable *and* is at or
    below the threshold. An unknown machine is not a quiet machine; it is a
    machine nothing is known about, and a reader must be told that rather than
    reassured.
    """
    monkeypatch.setattr(quiet_box, "load_per_core", lambda: None)

    assert quiet_box.is_quiet() is False


def test_the_threshold_is_inclusive_at_parity_and_excludes_anything_above_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Parity - the run queue as long as the machine is wide - is the last quiet value.

    Asserted in both directions because only the upper one catches a comparison
    that was widened, and only the lower one catches one that was narrowed.
    """
    monkeypatch.setattr(quiet_box, "load_per_core", lambda: quiet_box.QUIET_BOX_LOAD_PER_CORE)
    assert quiet_box.is_quiet() is True

    monkeypatch.setattr(quiet_box, "load_per_core", lambda: quiet_box.QUIET_BOX_LOAD_PER_CORE + 0.01)
    assert quiet_box.is_quiet() is False


def test_the_stamp_names_the_load_the_threshold_and_the_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A stamp that carried only a verdict would be unauditable after the fact.

    The number and the threshold are both in the line because the threshold is a
    judgement call that may be revised, and a transcript pasted into a report
    six weeks later has to stay readable against whatever the constant is then.
    """
    monkeypatch.setattr(quiet_box, "load_per_core", lambda: 0.25)

    line = quiet_box.stamp("before")

    assert line.startswith("QUIET BOX [before]:"), line
    assert "0.25 load per core" in line, line
    assert f"threshold {quiet_box.QUIET_BOX_LOAD_PER_CORE:.2f}" in line, line
    assert line.endswith("- quiet-box verified"), line
    assert len(line.splitlines()) == 1, f"the stamp is not one line: {line!r}"


def test_a_contended_stamp_says_so_rather_than_only_omitting_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The negative direction, which is the one a reader acts on.

    "NOT quiet-box verified" has to be present as text: a stamp that merely
    dropped the word "verified" reads as a formatting variation to a human
    skimming a long transcript, and a result is then trusted by omission.
    """
    monkeypatch.setattr(quiet_box, "load_per_core", lambda: 11.5)

    line = quiet_box.stamp("after")

    assert "NOT quiet-box verified" in line, line
    assert "11.50 load per core" in line, line


def test_the_stamp_says_not_verified_when_the_load_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    An unknown load must produce a line, and that line must not read as a pass.

    Raising instead would take out the run being annotated; printing a number
    would invent one. The stamp says what it does not know.
    """
    monkeypatch.setattr(quiet_box, "load_per_core", lambda: None)

    line = quiet_box.stamp("before")

    assert "load average unavailable" in line, line
    assert "NOT quiet-box verified" in line, line
    assert "load per core" not in line, f"the stamp invented a number it does not have: {line!r}"


def test_the_by_path_loader_refuses_a_caller_outside_the_repository(tmp_path: Path) -> None:
    """
    A scenario copied out of the tree must fail loudly rather than silently unstamped.

    `load_quiet_box` walks upwards for `scripts/quiet_box.py`. Returning None -
    or a stub - when it finds nothing would leave a rig transcript with no load
    stamp at all, which is indistinguishable from a transcript taken on a quiet
    box by anyone reading it later.
    """
    spec = importlib.util.spec_from_file_location("quiet_box_bootstrap_negative", QUIET_BOX_PATH)
    assert spec is not None and spec.loader is not None
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)

    with pytest.raises(FileNotFoundError):
        bootstrap.load_quiet_box(tmp_path / "somewhere" / "scenario.py")
