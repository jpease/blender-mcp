"""
Tests for `scripts/render_coverage.py`, which names the render properties no tool can reach.

`scripts/` is outside `testpaths`, so nothing else exercises this splitter.
"""

import sys

from conftest import REPO_ROOT

# Appended so `scripts/` cannot shadow a stdlib module for the rest of the session.
sys.path.append(str(REPO_ROOT / "scripts"))

import render_coverage

# Two owners carrying the same identifier, which is the case a set of bare property names
# gets wrong: `use_denoise` exists on the EEVEE ray-tracing struct and on Cycles' own.
_BASELINE = """=== BLENDER === 5.2.2 LTS
frame_range: 1 250
owner: scene.render
  engine
  use_denoise
  threads
owner: scene.cycles
  use_denoise
owner: scene.view_settings
  (absent)
"""


def test_coverage_classifies_the_same_identifier_per_owner() -> None:
    """Reaching `scene.cycles.use_denoise` says nothing about `scene.render.use_denoise`."""
    report = render_coverage.coverage(_BASELINE, {("scene.cycles", "use_denoise")}, {})

    assert report["reachable"] == ["scene.cycles:use_denoise"]
    assert "scene.render:use_denoise" in report["unreachable"]


def test_coverage_never_reports_an_excluded_property_as_unreachable() -> None:
    """An excluded property is a decision with a reason, not a gap."""
    report = render_coverage.coverage(
        _BASELINE,
        {("scene.render", "engine")},
        {("scene.render", "threads"): "a machine-local scheduling choice"},
    )

    assert report["excluded"] == ["scene.render:threads - a machine-local scheduling choice"]
    assert not [entry for entry in report["unreachable"] if entry.startswith("scene.render:threads")]


def test_coverage_ignores_an_absent_owner() -> None:
    """A build without Cycles still produces a stable transcript, and `(absent)` is not a property."""
    report = render_coverage.coverage(_BASELINE, set(), {})

    assert not [entry for entry in report["unreachable"] if "absent" in entry]
    assert "scene.view_settings:(absent)" not in report["unreachable"]


def test_the_recorded_baseline_and_the_shipped_routes_agree_on_the_reachable_set() -> None:
    """
    The real transcript and the real routing table must intersect, not merely parse.

    An empty intersection means a route names an owner or identifier the probe never saw,
    which is how this report silently becomes "nothing is reachable".
    """
    report = render_coverage.coverage(
        render_coverage._DEFAULT_BASELINE.read_text(encoding="utf-8"),
        render_coverage.reachable_properties(),
        render_coverage.EXCLUDED,
    )

    assert "scene.render:engine" in report["reachable"]
    assert "scene.render.image_settings:file_format" in report["reachable"]
    assert "scene.eevee.ray_tracing_options:use_denoise" in report["reachable"]
    assert len(report["excluded"]) == len(render_coverage.EXCLUDED)
