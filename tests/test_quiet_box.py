"""
Tests for `scripts/quiet_box.py`, the load stamp on wall-clock artifacts.

A bug here fails silently: it labels a contended run verified and a reader trusts
an inflated number. Every path that yields "unknown" must never read as quiet.
"""

from __future__ import annotations

import importlib.util
import os

from pathlib import Path
from types import ModuleType

import pytest

QUIET_BOX_PATH = Path(__file__).resolve().parents[1] / "scripts" / "quiet_box.py"
# A load of 8.0 across 4 cores, so the expected quotient is exact in binary.
_STUB_ONE_MINUTE_LOAD = 8.0
_STUB_CPU_COUNT = 4
_EXPECTED_PER_CORE = _STUB_ONE_MINUTE_LOAD / _STUB_CPU_COUNT


def _load_quiet_box() -> ModuleType:
    """
    Load the module under test through its own by-path loader, which nothing else covers.

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
    """The 1-minute figure, per core, so it describes the current run on any machine size."""
    monkeypatch.setattr(os, "getloadavg", lambda: (_STUB_ONE_MINUTE_LOAD, 99.0, 99.0))
    monkeypatch.setattr(os, "cpu_count", lambda: _STUB_CPU_COUNT)

    assert quiet_box.load_per_core() == pytest.approx(_EXPECTED_PER_CORE)


def test_an_unreadable_load_average_is_unknown_not_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """A platform without a load average gives None, not 0.0, which would read as idle."""

    def unavailable() -> tuple[float, float, float]:
        raise OSError("load average is not available on this platform")

    monkeypatch.setattr(os, "getloadavg", unavailable)

    assert quiet_box.load_per_core() is None


def test_a_cpu_count_of_zero_is_unknown_rather_than_a_division_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CPU count of 0 is treated like None, so the stamp cannot crash the run it annotates."""
    monkeypatch.setattr(os, "getloadavg", lambda: (1.0, 1.0, 1.0))
    monkeypatch.setattr(os, "cpu_count", lambda: 0)

    assert quiet_box.load_per_core() is None


def test_an_unknown_load_is_never_reported_as_quiet_box_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown load is not quiet."""
    monkeypatch.setattr(quiet_box, "load_per_core", lambda: None)

    assert quiet_box.is_quiet() is False


def test_the_threshold_is_inclusive_at_parity_and_excludes_anything_above_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The threshold itself is quiet and anything above it is not, catching a comparison off by one either way."""
    monkeypatch.setattr(quiet_box, "load_per_core", lambda: quiet_box.QUIET_BOX_LOAD_PER_CORE)
    assert quiet_box.is_quiet() is True

    monkeypatch.setattr(quiet_box, "load_per_core", lambda: quiet_box.QUIET_BOX_LOAD_PER_CORE + 0.01)
    assert quiet_box.is_quiet() is False


def test_the_stamp_names_the_load_the_threshold_and_the_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stamp carries the number and threshold, so an old transcript stays readable if the threshold changes."""
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
    """A contended run says "NOT quiet-box verified", which a skimming reader cannot miss."""
    monkeypatch.setattr(quiet_box, "load_per_core", lambda: 11.5)

    line = quiet_box.stamp("after")

    assert "NOT quiet-box verified" in line, line
    assert "11.50 load per core" in line, line


def test_the_stamp_says_not_verified_when_the_load_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown load still produces a line, with no invented number and no pass."""
    monkeypatch.setattr(quiet_box, "load_per_core", lambda: None)

    line = quiet_box.stamp("before")

    assert "load average unavailable" in line, line
    assert "NOT quiet-box verified" in line, line
    assert "load per core" not in line, f"the stamp invented a number it does not have: {line!r}"


def test_the_by_path_loader_refuses_a_caller_outside_the_repository(tmp_path: Path) -> None:
    """A scenario copied out of the repository fails, instead of running without a load stamp."""
    spec = importlib.util.spec_from_file_location("quiet_box_bootstrap_negative", QUIET_BOX_PATH)
    assert spec is not None and spec.loader is not None
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)

    with pytest.raises(FileNotFoundError):
        bootstrap.load_quiet_box(tmp_path / "somewhere" / "scenario.py")
