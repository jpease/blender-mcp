"""
Regenerate `src/blender_mcp/addon_surface.json`, the committed snapshot of the add-on's dispatch surface.

Run this after adding, removing or re-signing any command handler, together with a
bump of `ADDON_PROTOCOL_VERSION` (bundled/addon/__init__.py) and
`EXPECTED_ADDON_PROTOCOL_VERSION` (addon_manager.py). `tests/test_addon_surface.py`
fails until you do, which is the point: before this snapshot existed, a command
could be added without moving the protocol number, and the resulting stale install
answered the handshake with a number that looked current while refusing the new
command.

The surface itself is built by `tests/test_addon_surface.build_addon_surface`, not
here, so the file this writes and the file that test compares against can never be
computed two different ways. That helper needs a fake `bpy` (the add-on cannot be
imported outside Blender) and gets it from the suite's one loader, so this script
puts `tests/` on the path and borrows pytest's `MonkeyPatch` outside a test run.

Usage:
    just addon-surface
    python scripts/update_addon_surface.py

"""

import sys

from pathlib import Path

import pytest

from blender_mcp.addon_manager import ADDON_SURFACE_PATH

_REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Rebuild the snapshot from the bundled add-on and report what it now records."""
    # Necessarily deferred: `tests/` is not a package and not on the path, so the import
    # cannot be hoisted above the line that makes it resolvable.
    sys.path.insert(0, str(_REPO_ROOT / "tests"))
    from test_addon_surface import build_addon_surface, render_addon_surface  # ruff: ignore[import-outside-top-level]

    with pytest.MonkeyPatch.context() as monkeypatch:
        surface = build_addon_surface(monkeypatch)

    ADDON_SURFACE_PATH.write_text(render_addon_surface(surface), encoding="utf-8")
    print(f"wrote {ADDON_SURFACE_PATH.relative_to(_REPO_ROOT)}")
    print(f"  protocol_version: {surface['protocol_version']}")
    print(f"  commands:         {len(surface['commands'])}")


if __name__ == "__main__":
    main()
