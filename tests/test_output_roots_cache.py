"""
Coverage for keeping the writable-output-root probe off Blender's hot path.

Every handshake runs on Blender's main thread, and a stat on a hung network mount
blocks there, freezing the UI and the command queue. So the probe runs once per
candidate list, and these tests also check that caching changes nothing about which
roots are advertised or in what order.
"""

from __future__ import annotations

import os
import sys

from pathlib import Path
from types import ModuleType

import pytest

from conftest import load_addon


def _instrumented_server(monkeypatch: pytest.MonkeyPatch) -> tuple[object, list[tuple[str | None, ...]], ModuleType]:
    """
    Load the addon with every filesystem probe of the candidate roots recorded.

    Counts calls to `writable_roots`, one per probe pass, rather than the stat calls
    inside it, so the count survives changes to the per-candidate checks.

    Args:
        monkeypatch: Fixture used to load the addon and install the counter.

    Returns:
        tuple: The server, the list of recorded probe passes (one tuple of
        candidates per pass), and the loaded `server_core` module.

    """
    addon, _bpy = load_addon(monkeypatch, data={"filepath": ""})
    server_core = sys.modules[f"{addon.__name__}.server_core"]
    real_writable_roots = server_core.writable_roots
    probes: list[tuple[str | None, ...]] = []

    def counting_writable_roots(candidates: tuple[str | None, ...]) -> list[str]:
        probes.append(tuple(candidates))
        return real_writable_roots(candidates)

    monkeypatch.setattr(server_core, "writable_roots", counting_writable_roots)
    return server_core.BlenderMCPServer(), probes, server_core


def test_a_repeated_handshake_does_not_probe_the_filesystem_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every MCP connection handshakes, so the probe must not be per-connection."""
    server, probes, _server_core = _instrumented_server(monkeypatch)

    server.get_addon_info()
    server.get_addon_info()
    server.get_addon_info()

    assert len(probes) == 1, f"the handshake probed the filesystem {len(probes)} times"


def test_the_cache_changes_neither_the_advertised_roots_nor_their_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Caching must not change which roots are advertised or their order.

    Both handshakes must match an uncached probe, with the configured root first.
    """
    mounted = tmp_path / "output"
    mounted.mkdir()
    monkeypatch.setenv("BLENDERMCP_OUTPUT_ROOTS", str(mounted))

    server, probes, server_core = _instrumented_server(monkeypatch)

    first = server.get_addon_info()["writable_output_roots"]
    second = server.get_addon_info()["writable_output_roots"]
    uncached = server_core.writable_roots(probes[0])

    assert first == uncached, "the cached answer differs from an uncached probe"
    assert second == first, "a repeated handshake reported different roots"
    assert first[0] == str(mounted), "the configured root must still lead the ranking"


def test_a_changed_candidate_list_is_probed_again(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    The cache is keyed on the candidate list, so a changed list is probed again.

    The open .blend and `BLENDERMCP_OUTPUT_ROOTS` can both change while the process
    runs; a cache for the process lifetime would advertise stale roots.
    """
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()

    monkeypatch.setenv("BLENDERMCP_OUTPUT_ROOTS", str(first_root))
    server, probes, _server_core = _instrumented_server(monkeypatch)
    assert server.get_addon_info()["writable_output_roots"][0] == str(first_root)

    monkeypatch.setenv("BLENDERMCP_OUTPUT_ROOTS", str(second_root))
    refreshed = server.get_addon_info()["writable_output_roots"]

    # Unpacking asserts exactly two probe passes, and that each saw its own root.
    first_pass, second_pass = probes
    assert str(first_root) in first_pass
    assert str(second_root) in second_pass, "a changed candidate list was answered from a stale cache"
    assert refreshed[0] == str(second_root), "the handshake advertised a superseded root"


def test_the_cache_cannot_be_corrupted_by_its_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A handshake mutating the list it was handed must not poison the next one.

    The roots go into a response dict other code may edit, so the cache must not
    hand out its own list.
    """
    server, _probes, _server_core = _instrumented_server(monkeypatch)

    roots = server.get_addon_info()["writable_output_roots"]
    expected = list(roots)
    roots.append(os.sep)
    roots.clear()

    assert server.get_addon_info()["writable_output_roots"] == expected
