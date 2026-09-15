"""
Coverage for keeping the writable-output-root probe off Blender's hot path.

`get_addon_info` is dispatched from `drain_command_queue`, i.e. on Blender's
main thread, and `writable_roots` runs an `os.path.isdir` plus an `os.access`
on every candidate - the first of which come from the operator-supplied
`BLENDERMCP_OUTPUT_ROOTS`. On a hung network mount those stat calls block
uninterruptibly, freezing Blender's UI and every queued command;
`_DRAIN_TIME_BUDGET_SECONDS` cannot bound them because that budget is only
checked *between* commands. So the answer is computed once per candidate list
instead of on every handshake.

The advertised roots and their order are a separate design decision (see
tests/test_output_roots.py and tests/test_blender_rig.py), so these tests also
pin the cache to changing nothing observable about *what* is advertised.
"""

from __future__ import annotations

import os
import sys

from pathlib import Path
from types import ModuleType

import pytest

from test_mutation_transaction import _load_addon  # ruff: ignore[import-private-name]


def _instrumented_server(monkeypatch: pytest.MonkeyPatch) -> tuple[object, list[tuple[str | None, ...]], ModuleType]:
    """
    Load the addon with every filesystem probe of the candidate roots recorded.

    The counter wraps `writable_roots` rather than `os.path.isdir`/`os.access`
    directly: one entry per *probe pass* is what the handshake cost is measured
    in, and it stays readable if the per-candidate checks are ever changed.

    Args:
        monkeypatch: Fixture used to load the addon and install the counter.

    Returns:
        tuple: The server, the list of recorded probe passes (one tuple of
        candidates per pass), and the loaded `server_core` module.

    """
    addon, _bpy = _load_addon(monkeypatch, data={"filepath": ""})
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
    Caching is an I/O optimisation; the ranking it serves is a design decision.

    Both handshakes must report exactly what an uncached probe of the same
    candidates would, in the same order - a configured root first.
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
    The candidates are not constant, so the cache must be keyed on them.

    `bpy.data.filepath` changes whenever the user opens or saves a .blend, and
    `BLENDERMCP_OUTPUT_ROOTS` can be re-exported; caching the first answer for
    the life of the process would keep advertising roots that no longer reflect
    either. Keying on the candidate list costs no I/O - building it is pure
    string work - and re-probes exactly when the answer could have changed.
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

    The roots travel into a response dict that other code is free to edit, and
    a cache that hands out its own list would then advertise whatever that
    caller left behind.
    """
    server, _probes, _server_core = _instrumented_server(monkeypatch)

    roots = server.get_addon_info()["writable_output_roots"]
    expected = list(roots)
    roots.append(os.sep)
    roots.clear()

    assert server.get_addon_info()["writable_output_roots"] == expected
