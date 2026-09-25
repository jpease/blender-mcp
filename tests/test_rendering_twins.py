"""
An animation's render reply is built on both sides of the socket and must come out the same.

`render_scene(mode="ANIMATION")` builds its reply in the add-on from its own render loop; the
orchestrated path in `server/tools/rendering.py` builds it from N per-frame STILL replies. The
add-on is installed into Blender as a self-contained package and the server must not import from
it, so `_PROGRESS_ENTRY_LIMIT`, `_STILL_DURATION_REFUSAL`, `_duration_overrun_warnings` and
`_animation_summary` are stated on each side. A change to one copy only is a reply that depends on
which path drove the render; these tests make it a failure here instead.
"""

import importlib

import pytest

from conftest import load_addon

from blender_mcp.server.tools import rendering as server_rendering


@pytest.fixture(name="addon_rendering")
def _addon_rendering(monkeypatch):
    """Load the add-on's `handlers/rendering.py` against a stub `bpy`."""
    addon, _bpy = load_addon(monkeypatch)
    return importlib.import_module(f"{addon.__name__}.handlers.rendering")


@pytest.mark.parametrize("name", ["_PROGRESS_ENTRY_LIMIT", "_STILL_DURATION_REFUSAL"])
def test_both_sides_hold_the_same_constant(addon_rendering, name) -> None:
    assert getattr(addon_rendering, name) == getattr(server_rendering, name)


@pytest.mark.parametrize(
    ("duration_seconds", "max_duration_seconds", "overran"),
    [
        (1.0, None, False),
        (1.0, 2.0, False),
        (2.0, 2.0, False),
        (2.05, 2, True),
        (123.456, 0.5, True),
    ],
)
def test_both_sides_warn_about_the_same_overrun(
    addon_rendering, duration_seconds, max_duration_seconds, overran
) -> None:
    server = server_rendering._duration_overrun_warnings(duration_seconds, max_duration_seconds)

    assert addon_rendering._duration_overrun_warnings(duration_seconds, max_duration_seconds) == server
    assert bool(server) is overran


def _files(count, *, missing=()):
    return [
        {"frame": frame, "path": f"/renders/shot_{frame:04d}.png", "bytes": None if frame in missing else 100 + frame}
        for frame in range(1, count + 1)
    ]


def _summary_arguments(*, files, frame_total, detail, cancelled=False, persisted=False, warnings=()):
    return {
        "scene_name": "Scene",
        "mode": "ANIMATION",
        "output": "/renders/shot_",
        "frame": 1,
        "engine": "CYCLES" if files else None,
        "effective_cycles_device": "CPU" if files else None,
        "warnings": list(warnings),
        "files": files,
        "frame_total": frame_total,
        "operator_result": ["FINISHED"],
        "persisted": persisted,
        "cancelled": cancelled,
        "cancellation_reason": "max_duration_seconds exceeded" if cancelled else None,
        "duration_seconds": 1.5,
        "render_slot_policy": "NEW_SLOT",
        "passes": [{"name": "Combined"}],
        "pass_verification": "render_result",
        "created_directory": True,
        "detail": detail,
    }


_OVER_THE_PROGRESS_LIMIT = server_rendering._PROGRESS_ENTRY_LIMIT + 1


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(_summary_arguments(files=[], frame_total=3, detail=True, cancelled=True), id="nothing-rendered"),
        pytest.param(
            _summary_arguments(files=_files(3, missing={2}), frame_total=3, detail=False, warnings=["GPU fell back"]),
            id="summary-with-warnings",
        ),
        pytest.param(
            _summary_arguments(files=_files(2), frame_total=5, detail=True, cancelled=True), id="cancelled-detail"
        ),
        pytest.param(_summary_arguments(files=_files(4), frame_total=4, detail=True, persisted=True), id="persisted"),
        pytest.param(
            _summary_arguments(
                files=_files(_OVER_THE_PROGRESS_LIMIT), frame_total=_OVER_THE_PROGRESS_LIMIT, detail=True
            ),
            id="progress-truncated",
        ),
    ],
)
def test_both_sides_build_the_same_reply(addon_rendering, arguments) -> None:
    server = server_rendering._animation_summary(**arguments)

    assert addon_rendering._animation_summary(**arguments) == server
