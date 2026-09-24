"""
Report which render properties the tool schemas can reach, and which they cannot.

Reads the recorded `render_property_coverage` probe transcript (every writable property on
`scene.render`, its image settings, EEVEE, Cycles and the view settings) and splits it three
ways against the routes in `src/blender_mcp/bundled/addon/render_properties.py`:

  reachable   a patch key writes it today
  excluded    deliberately out of the tool surface, with the reason recorded below
  unreachable nothing in the MCP can set it

Everything is keyed by `(owner, rna_identifier)`: the same identifier exists on several owners,
and patch keys (`image_format`, `enabled`, `transparent`) are not RNA identifiers at all.

Needs no Blender: the transcript is committed, and the routing table is a bpy-free leaf module
loaded by path so importing it does not execute the add-on package.

Usage::

    .venv/bin/python scripts/render_coverage.py [BASELINE] [--strict]

The default run is a report, like the probes it reads. `--strict` exits 1 when anything is
unreachable, for a branch that means to keep the list empty.
"""

import argparse
import importlib.util
import json
import sys

from collections.abc import Mapping, Set
from pathlib import Path
from types import ModuleType

from pydantic import BaseModel

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_BASELINE = _REPO_ROOT / "scripts" / "blender_probes" / "baselines" / "render_property_coverage.txt"
_ROUTING_TABLE = _REPO_ROOT / "src" / "blender_mcp" / "bundled" / "addon" / "render_properties.py"

# Owner keys the probe prints, mapped from the route paths `render_properties` uses.
_OWNER_FOR_ROUTE = {
    "render": "scene.render",
    "render.image_settings": "scene.render.image_settings",
    "render.image_settings.stereo_3d_format": "scene.render.image_settings.stereo_3d_format",
    "cycles": "scene.cycles",
    "eevee": "scene.eevee",
    "eevee.ray_tracing_options": "scene.eevee.ray_tracing_options",
}

# A property the tool surface deliberately does not expose, and why. One line each, so a
# reviewer can disagree with the reason rather than guess at the omission.
EXCLUDED: Mapping[tuple[str, str], str] = {
    ("scene.render", "use_border"): "a border render writes a partial frame that reads as a failed render",
    ("scene.render", "use_crop_to_border"): "only meaningful with use_border, which is excluded",
    ("scene.render", "threads"): "a machine-local scheduling choice, not shot intent, and it does not travel",
    ("scene.render", "threads_mode"): "same: the thread count belongs to the host, not the .blend",
}


def _load_routing_table() -> ModuleType:
    """
    Load `render_properties.py` by path, without importing the add-on package.

    Returns:
        The loaded module.

    Raises:
        SystemExit: When the module cannot be loaded.

    """
    spec = importlib.util.spec_from_file_location("blender_mcp_render_properties", _ROUTING_TABLE)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load the routing table at {_ROUTING_TABLE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _section_models() -> Mapping[str, type[BaseModel]]:
    """
    Map each identity-routed section to the pydantic model that names its keys.

    An identity route's patch keys are the RNA identifiers, so the tool schema is where the
    reachable set is written down.

    Returns:
        Mapping[str, type[BaseModel]]: Section name to model class.

    """
    # ruff: ignore[import-outside-top-level] - importing FastMCP is this lookup's whole cost
    from blender_mcp.server.tools.rendering import (
        CyclesPatch,
        EeveePatch,
        EeveeRayTracingPatch,
        MetadataPatch,
        MultiviewPatch,
        PerformancePatch,
    )

    return {
        "metadata": MetadataPatch,
        "multiview": MultiviewPatch,
        "cycles": CyclesPatch,
        "eevee": EeveePatch,
        "eevee.ray_tracing": EeveeRayTracingPatch,
        "performance": PerformancePatch,
    }


def reachable_properties() -> set[tuple[str, str]]:
    """
    Derive every `(owner, rna_identifier)` a render-settings or lighting-quality patch can write.

    Flat `SCENE_PROPERTIES` (frame_start/frame_end/frame_step) are dropped: they live on the
    Scene, which this probe does not enumerate, and attributing them to `scene.render` would
    be a lie that hides a real gap. `configure_lighting_quality`'s fields count too, so a
    property it already sets (`scene.cycles.device`) is not reported as a gap to fill again.

    Returns:
        set[tuple[str, str]]: Owner key and RNA identifier pairs.

    """
    table = _load_routing_table()
    models = _section_models()
    reachable = {("scene.render", name) for name in table.RENDER_PROPERTIES}
    reachable |= {("scene.render.image_settings", name) for name in table.IMAGE_PROPERTY_MAPPING.values()}
    reachable |= {("scene.cycles", name) for name in table.CYCLES_PROPERTY_MAPPING.values()}
    reachable |= {("scene.cycles", name) for name in table.LIGHTING_CYCLES_FIELDS}
    reachable |= {("scene.eevee", name) for name in table.LIGHTING_EEVEE_FIELD_MAP.values()}
    for section, routes in table.NESTED_SECTIONS.items():
        claimed: set[str] = {key for _path, mapping, _label in routes if mapping is not None for key in mapping}
        for owner_path, mapping, _label in routes:
            owner = _OWNER_FOR_ROUTE[owner_path]
            if mapping is not None:
                reachable |= {(owner, identifier) for identifier in mapping.values()}
                continue
            model = models[section]
            reachable |= {(owner, field) for field in model.model_fields if field not in claimed}
    return reachable


def parse_baseline(text: str) -> set[tuple[str, str]]:
    """
    Read `(owner, identifier)` pairs out of a probe transcript.

    Args:
        text: The recorded transcript.

    Returns:
        set[tuple[str, str]]: Every probed property.

    Raises:
        SystemExit: When the transcript names no owner, which means it is not this probe's.

    """
    probed: set[tuple[str, str]] = set()
    owner: str | None = None
    for line in text.splitlines():
        if line.startswith("owner: "):
            owner = line[len("owner: ") :].strip()
            continue
        if not line.startswith("  ") or owner is None:
            continue
        identifier = line.strip()
        if identifier == "(absent)":
            continue
        probed.add((owner, identifier))
    if owner is None:
        raise SystemExit("the baseline names no owner: it is not a render_property_coverage transcript")
    return probed


def coverage(baseline_text: str, reachable: Set[tuple[str, str]], excluded: Mapping[tuple[str, str], str]) -> dict:
    """
    Split every probed property into reachable, excluded-with-reason, and unreachable.

    Args:
        baseline_text: The recorded probe transcript.
        reachable: Pairs a patch key writes.
        excluded: Pairs deliberately left out, mapped to the reason.

    Returns:
        dict: `reachable`, `excluded` and `unreachable`, each a sorted list of
        `owner:identifier` strings; `excluded` entries carry their reason.

    """
    probed = parse_baseline(baseline_text)
    reached = sorted(f"{owner}:{name}" for owner, name in probed & set(reachable))
    left_out = sorted(f"{owner}:{name} - {excluded[owner, name]}" for owner, name in probed & set(excluded))
    unreachable = sorted(f"{owner}:{name}" for owner, name in probed - set(reachable) - set(excluded))
    return {"reachable": reached, "excluded": left_out, "unreachable": unreachable}


def main(argv: list[str] | None = None) -> int:
    """
    Print the three groups, newest measurement first.

    Args:
        argv: Command-line arguments, or None for `sys.argv[1:]`.

    Returns:
        int: 0, or 1 under `--strict` with a non-empty unreachable group.

    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", nargs="?", default=str(_DEFAULT_BASELINE))
    parser.add_argument("--strict", action="store_true", help="exit 1 when anything is unreachable")
    parser.add_argument("--json", action="store_true", help="print the three groups as JSON")
    args = parser.parse_args(argv)

    baseline = Path(args.baseline)
    if not baseline.is_file():
        print(f"no baseline at {baseline}; record it with: just probes-record --only render_property_coverage")
        return 1
    report = coverage(baseline.read_text(encoding="utf-8"), reachable_properties(), EXCLUDED)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for group in ("reachable", "excluded", "unreachable"):
            print(f"\n{group} ({len(report[group])})")
            for entry in report[group]:
                print(f"  {entry}")
        print()
    return 1 if args.strict and report["unreachable"] else 0


if __name__ == "__main__":
    sys.exit(main())
