"""The cache frame-range write cloth, liquid and rigid bodies share."""

import sys

from conftest import load_addon


def _simulation_cache(monkeypatch):
    addon, _bpy = load_addon(monkeypatch, data={})
    return sys.modules[f"{addon.__name__}.handlers.simulation_cache"]


def test_a_range_past_the_current_end_writes_the_end_first(monkeypatch) -> None:
    simulation_cache = _simulation_cache(monkeypatch)
    writes = []

    class Cache:
        frame_start = 1
        frame_end = 20

        def __setattr__(self, name, value) -> None:
            writes.append((name, value))
            object.__setattr__(self, name, value)

    cache = Cache()
    simulation_cache.set_cache_frame_range(cache, 30, 60)

    assert writes == [("frame_end", 60), ("frame_start", 30)]
    assert (cache.frame_start, cache.frame_end) == (30, 60)
