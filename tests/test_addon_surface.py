"""
Make a change to the add-on's dispatch surface impossible to land without a protocol bump.

An agent asked for `solve_bone_reach` and was told `'solve_bone_reach' is not
supported by the installed Blender addon (protocol 31)`, concluded the feature did
not exist in the project, and worked around it. It did exist: the handler landed in
`0e5c461` (2026-09-20), while `ADDON_PROTOCOL_VERSION` had last moved to 31 in
`2852803` (2026-09-15). So the installed add-on was an actual protocol-31 build that
simply predated the command, the handshake compared 31 against an expected 31, and
`up_to_date` came back True - nothing anywhere suggested reinstalling. The runtime
guards behaved correctly; what failed was the freshness signal, because an integer
bumped by hand cannot notice that the dispatch table grew underneath it.

`src/blender_mcp/addon_surface.json` is that missing memory: a committed snapshot of
every command and accepted keyword the bundled add-on dispatches. The test below
fails the moment the live surface drifts from it, so the protocol number cannot stay
still through a surface change - which is what makes the handshake's comparison, and
`addon_manager`'s new same-protocol gap check, trustworthy again.

Regenerate with `just addon-surface` (`scripts/update_addon_surface.py`), which
imports its surface builder from this module so the snapshot and the assertion can
never be computed two different ways.
"""

import json
import sys

from typing import TypedDict

from test_mutation_transaction import _load_addon

from blender_mcp.addon_manager import (
    ADDON_SURFACE_PATH,
    EXPECTED_ADDON_PROTOCOL_VERSION,
    get_bundled_addon_path,
    read_addon_protocol_version,
)

# A drift report names this many commands before collapsing into a count. Renaming a
# shared parameter can move hundreds of entries at once, and a wall of names buries
# the instruction at the bottom of the message.
_MAX_LISTED_NAMES = 5
_MAX_LISTED_COMMANDS = 20

_REGENERATE = (
    "The add-on's dispatch surface changed. Bump ADDON_PROTOCOL_VERSION in "
    "src/blender_mcp/bundled/addon/__init__.py *and* EXPECTED_ADDON_PROTOCOL_VERSION in "
    "src/blender_mcp/addon_manager.py to the same new number, then run `just addon-surface` "
    "to regenerate src/blender_mcp/addon_surface.json."
)


class AddonSurface(TypedDict):
    """The snapshot document: the protocol number it belongs to, and the surface it records."""

    protocol_version: int
    # ACCEPTS_ANY_KEYWORD ("*") stands in for a handler whose keywords cannot be enumerated.
    commands: dict[str, list[str] | str]


def build_addon_surface(monkeypatch) -> AddonSurface:
    """
    Read the live dispatch surface straight out of the bundled add-on.

    Source of truth is `BlenderMCPServer._build_command_handlers()` fed through the
    add-on's own `capability_params` - byte for byte what `get_addon_info` publishes
    to the handshake, so the snapshot is comparable to what a running add-on reports
    rather than to a second, hand-maintained list.

    The fake `bpy` leaves `blendermcp_use_polyhaven`/`_sketchfab`/`_nd` all False, so
    the snapshot holds the *unconditional* surface. That is deliberate: the
    provider-gated handlers come and go with the open .blend's scene flags, and a
    snapshot that expected them would accuse every user who has those integrations
    switched off of running a stale add-on.

    Args:
        monkeypatch: Fixture (or a bare `pytest.MonkeyPatch`) the add-on loader
            installs its `bpy` stubs through.

    Returns:
        AddonSurface: The protocol number this surface belongs to, and every
        dispatchable command with the keywords it accepts.

    """
    addon, _bpy = _load_addon(monkeypatch, data={})
    server_core = sys.modules[f"{addon.__name__}.server_core"]
    introspection = sys.modules[f"{addon.__name__}.capability_introspection"]
    handlers = server_core.BlenderMCPServer()._build_command_handlers()
    return AddonSurface(
        protocol_version=EXPECTED_ADDON_PROTOCOL_VERSION,
        commands=introspection.capability_params(handlers),
    )


def render_addon_surface(surface: AddonSurface) -> str:
    """
    Serialize a surface the one way the committed file is allowed to look.

    Sorted keys and one entry per line: the whole value of committing this file is
    that adding a command shows up in review as a one-line diff rather than as a
    reflowed blob.

    Args:
        surface: The document `build_addon_surface` returns.

    Returns:
        str: JSON text, ending in a newline.

    """
    return json.dumps(surface, indent=2, sort_keys=True) + "\n"


def _sample(names: list[str]) -> str:
    """
    Render a name list short enough to read, with the overflow counted.

    Args:
        names: The names to list.

    Returns:
        str: Comma-separated names, truncated with a `(+N more)` tail.

    """
    head = ", ".join(names[:_MAX_LISTED_NAMES])
    extra = len(names) - _MAX_LISTED_NAMES
    return f"{head} (+{extra} more)" if extra > 0 else head


def describe_surface_drift(committed: dict[str, list[str] | str], live: dict[str, list[str] | str]) -> list[str]:
    """
    Spell out how a live surface differs from the committed one, in reviewable lines.

    Args:
        committed: The `commands` mapping read from `addon_surface.json`.
        live: The `commands` mapping just built from the bundled add-on.

    Returns:
        list[str]: One line per difference, closing with the regeneration
        instruction; empty when the two agree exactly.

    """
    lines: list[str] = []
    added = sorted(set(live) - set(committed))
    removed = sorted(set(committed) - set(live))
    if added:
        lines.append(f"commands added ({len(added)}): {_sample(added)}")
    if removed:
        lines.append(f"commands removed ({len(removed)}): {_sample(removed)}")

    changed = [name for name in sorted(set(committed) & set(live)) if committed[name] != live[name]]
    for name in changed[:_MAX_LISTED_COMMANDS]:
        before, after = committed[name], live[name]
        if not isinstance(before, list) or not isinstance(after, list):
            # One side is the ACCEPTS_ANY_KEYWORD sentinel: the handler gained or lost
            # **kwargs, which flips connection.py's preflight gate between filtering
            # and waving everything through, so it is worth naming as itself.
            lines.append(f"{name}: keyword reporting changed from {before!r} to {after!r}")
            continue
        gained = sorted(set(after) - set(before))
        lost = sorted(set(before) - set(after))
        detail = "; ".join(
            part
            for part in (
                f"parameters added: {_sample(gained)}" if gained else "",
                f"parameters removed: {_sample(lost)}" if lost else "",
            )
            if part
        )
        lines.append(f"{name}: {detail}")
    if len(changed) > _MAX_LISTED_COMMANDS:
        lines.append(f"...and {len(changed) - _MAX_LISTED_COMMANDS} further commands whose parameters changed")

    if lines:
        lines.append(_REGENERATE)
    return lines


def _committed_surface() -> AddonSurface:
    """
    Read the committed snapshot.

    Returns:
        AddonSurface: The parsed `addon_surface.json` document.

    """
    return json.loads(ADDON_SURFACE_PATH.read_text(encoding="utf-8"))


def test_committed_surface_matches_the_live_dispatch_table(monkeypatch) -> None:
    """The snapshot is what the add-on actually dispatches, or the protocol number is a lie."""
    committed = _committed_surface()
    live = build_addon_surface(monkeypatch)

    drift = describe_surface_drift(committed["commands"], live["commands"])

    assert not drift, "\n".join(["", *drift])


def test_committed_surface_is_serialized_the_way_the_generator_writes_it(monkeypatch) -> None:
    """A hand-edited snapshot would drift from the generator's output and hide real diffs."""
    live = build_addon_surface(monkeypatch)

    assert ADDON_SURFACE_PATH.read_text(encoding="utf-8") == render_addon_surface(live), (
        f"src/blender_mcp/addon_surface.json is not what the generator would write. {_REGENERATE}"
    )


def test_snapshot_records_the_protocol_version_the_server_expects() -> None:
    """
    Pin the snapshot to a protocol number.

    Paired with the drift test above, this is the whole mechanism: a surface change
    fails the drift test, regenerating the snapshot rewrites this number, and a
    regeneration that did not bump the constant leaves the snapshot identical - so
    the surface change cannot reach a user behind an unchanged protocol version.
    """
    assert _committed_surface()["protocol_version"] == EXPECTED_ADDON_PROTOCOL_VERSION, _REGENERATE


def test_both_protocol_constants_agree() -> None:
    """
    The add-on's own constant and the server's expectation are compared numerically at runtime.

    Nothing else keeps them equal - they live in two files - and a handshake between
    a mismatched pair either nags about an add-on that is current or blesses one that
    is not.
    """
    bundled = read_addon_protocol_version(get_bundled_addon_path())

    assert bundled == EXPECTED_ADDON_PROTOCOL_VERSION, (
        f"bundled add-on declares ADDON_PROTOCOL_VERSION {bundled}, but addon_manager expects "
        f"{EXPECTED_ADDON_PROTOCOL_VERSION}; both must carry the same number."
    )
