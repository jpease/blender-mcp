"""Shared frame preflight and state-restoring evaluation for liquid handlers."""

from __future__ import annotations

import time

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol


class _Scene(Protocol):
    frame_start: int
    frame_end: int
    frame_current: int
    frame_subframe: float

    def frame_set(self, frame: int, subframe: float = 0.0) -> None: ...


class _ViewLayer(Protocol):
    def update(self) -> None: ...


class _CacheSettings(Protocol):
    cache_type: str
    cache_frame_start: int
    has_cache_baked_any: bool


@dataclass(frozen=True)
class _FrameEvaluationPlan:
    frames: list[int]
    is_replay: bool
    replay_start: int | None
    preroll_frames: int | None


def _plan_frame_evaluation(
    frames: Sequence[int],
    scene: _Scene,
    settings: _CacheSettings,
    *,
    baked_frame_ceiling: Callable[[_CacheSettings], int],
    max_frames: int = 32,
    max_preroll_frames: int = 250,
    operation: str = "Evaluating",
) -> _FrameEvaluationPlan:
    """Validate frame/cache constraints once for all liquid evaluation workflows."""
    normalized = _normalize_frames(frames, max_frames=max_frames)
    is_replay = settings.cache_type == "REPLAY"
    _require_cache_available(settings, operation=operation)
    if any(frame < scene.frame_start or frame > scene.frame_end for frame in normalized):
        raise ValueError("All frames must be inside the scene frame range")

    if not is_replay:
        ceiling = baked_frame_ceiling(settings)
        out_of_range = [frame for frame in normalized if frame < settings.cache_frame_start or frame > ceiling]
        if out_of_range:
            raise ValueError(
                f"Frames {out_of_range} are outside the baked cache range "
                f"[{settings.cache_frame_start}, {ceiling}] for cache_type={settings.cache_type}"
            )
        return _FrameEvaluationPlan(normalized, False, None, None)

    if normalized[0] < settings.cache_frame_start:
        raise ValueError(
            f"Requested frame {normalized[0]} is before cache_frame_start={settings.cache_frame_start}; "
            "REPLAY caching only advances forward from the start of its cache range"
        )
    preroll_frames = normalized[-1] - settings.cache_frame_start + 1
    if preroll_frames > max_preroll_frames:
        raise ValueError(
            f"{operation} frame {normalized[-1]} in REPLAY mode requires sequentially stepping through "
            f"{preroll_frames} frames from cache_frame_start={settings.cache_frame_start}; exceeds "
            f"max_preroll_frames={max_preroll_frames}"
        )
    return _FrameEvaluationPlan(normalized, True, settings.cache_frame_start, preroll_frames)


def _normalize_frames(frames: Sequence[int], *, max_frames: int = 32) -> list[int]:
    """Return ordered integer frames after enforcing the public request bound."""
    if not frames or len(frames) > max_frames or len(set(frames)) != len(frames):
        raise ValueError(f"frames must contain 1-{max_frames} unique frame numbers")
    return sorted(int(frame) for frame in frames)


def _require_cache_available(settings: _CacheSettings, *, operation: str) -> None:
    """Reject direct frame evaluation when no replay or baked cache is available."""
    if settings.cache_type != "REPLAY" and not settings.has_cache_baked_any:
        raise ValueError(f"{operation} requires REPLAY cache mode or an existing modular/final bake")


def _evaluate_frames[SampleT](
    scene: _Scene,
    view_layer: _ViewLayer,
    plan: _FrameEvaluationPlan,
    timeout_seconds: float,
    sample_frame: Callable[[int], SampleT],
) -> tuple[list[SampleT], bool]:
    """Evaluate a frame plan and restore the original timeline even when sampling fails."""
    original_frame = scene.frame_current
    original_subframe = scene.frame_subframe
    deadline = time.monotonic() + timeout_seconds
    requested = set(plan.frames)
    results = []
    timed_out = False
    replay_start = plan.replay_start if plan.replay_start is not None else plan.frames[0]
    evaluation_frames = range(replay_start, plan.frames[-1] + 1) if plan.is_replay else plan.frames
    try:
        for frame in evaluation_frames:
            scene.frame_set(frame)
            view_layer.update()
            if not plan.is_replay or frame in requested:
                results.append(sample_frame(frame))
            if time.monotonic() >= deadline and frame != plan.frames[-1]:
                timed_out = True
                break
    finally:
        scene.frame_set(original_frame, subframe=original_subframe)
        view_layer.update()
    return results, timed_out
